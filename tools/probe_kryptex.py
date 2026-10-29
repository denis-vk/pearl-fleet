#!/usr/bin/env python3
"""Probe the Kryptex Pearl pool to pin down its exact stratum dialect.

Kryptex does not document its wire protocol, so before flipping the fleet build
onto prl.kryptex.network run this once from any machine with network access:

    python tools/probe_kryptex.py prl.kryptex.network 8048 --wallet prl1TEST

What it does (all read-only for the pool -- it NEVER submits a share):
  1. TLS handshake (prints the cert), falling back to plain TCP if asked.
  2. Tries authorize variants (Pearl Stratum V1 combined / LuckyPool object /
     classic user+pass), printing every response verbatim.
  3. Listens for jobs and prints each raw frame (gunzipping if needed),
     summarizing the notify field names it saw.
  4. Prints the P40_KRYPTEX_* env values that match the observed dialect.
"""
from __future__ import annotations

import argparse
import gzip
import json
import socket
import ssl
import sys
import time

GZIP_MAGIC = b"\x1f\x8b"
DEFAULT_WALLET = "prl1probe0000000000000000000000000000000000000000probe"


def gunzip_maybe(frame: bytes) -> bytes:
    if frame[:2] == GZIP_MAGIC:
        try:
            return gzip.decompress(frame)
        except OSError:
            pass
    return frame


def read_frame(s, buf, timeout):
    s.settimeout(timeout)
    while b"\n" not in buf:
        try:
            d = s.recv(65536)
        except (socket.timeout, BlockingIOError):
            return None, buf
        if not d:
            return b"", buf
        buf += d
    line, buf = buf.split(b"\n", 1)
    return gunzip_maybe(line), buf


def send_frame(s, obj):
    payload = (json.dumps(obj) + "\n").encode()
    print(f">>> {payload.decode()[:400]}")
    s.sendall(payload)


def try_authorize(s, buf, wallet, worker):
    """Authorize variants, in order of likelihood. Returns (ok, buf)."""
    variants = [
        ("V1 combined  [\"wallet.worker\"]",
         {"id": 1, "method": "mining.authorize", "params": [f"{wallet}.{worker}"]}),
        ("LuckyPool object {wallet, worker}",
         {"id": 1, "method": "mining.authorize",
          "params": {"wallet": wallet, "worker": worker}}),
        ("classic user+pass",
         {"id": 1, "method": "mining.authorize", "params": [f"{wallet}.{worker}", "x"]}),
        ("login method",
         {"id": 1, "method": "login", "params": {"login": f"{wallet}.{worker}", "pass": "x"}}),
    ]
    for label, msg in variants:
        print(f"\n--- authorize variant: {label}")
        try:
            send_frame(s, msg)
        except (BrokenPipeError, ConnectionResetError) as e:
            print(f"connection died on send: {e}; pool likely closed after an "
                  f"unrecognized variant -- reconnect and continue with the next")
            return False, buf
        # wait up to 15s for a direct response OR a job push (job push == accepted)
        deadline = time.time() + 15
        while time.time() < deadline:
            frame, buf = read_frame(s, buf, min(5, max(deadline - time.time(), 0.5)))
            if frame is None:
                continue
            if frame == b"":
                print("pool closed the connection")
                return False, buf
            text = frame.decode("utf-8", "replace")
            print(f"<<< {text[:500]}")
            try:
                msg = json.loads(text)
            except ValueError:
                print("(not JSON -- protocol may be binary; capture with Wireshark "
                      "while krig runs and compare)")
                continue
            if msg.get("id") == 1:
                if msg.get("result") is True or msg.get("result") is None and msg.get("error") is None:
                    print(">>> AUTHORIZED with this variant -- stopping variant search")
                    return True, buf
                print(f"variant rejected: {msg.get('error')}")
                break
            if msg.get("method") == "mining.notify":
                print(">>> job push received without an explicit authorize result -- "
                      "treating as authorized")
                return True, buf
    return False, buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--wallet", default=DEFAULT_WALLET,
                    help="your real prl1... wallet (some pools drop probe wallets)")
    ap.add_argument("--worker", default="probe")
    ap.add_argument("--plain", action="store_true", help="no TLS")
    ap.add_argument("--listen", type=int, default=90,
                    help="seconds to listen for jobs after authorizing")
    args = ap.parse_args()

    raw = socket.create_connection((args.host, args.port), timeout=30)
    if args.plain:
        s = raw
        print(f"connected (plain TCP) to {args.host}:{args.port}")
    else:
        ctx = ssl.create_default_context()
        try:
            s = ctx.wrap_socket(raw, server_hostname=args.host)
            cert = s.getpeercert()
            print(f"TLS OK: {args.host}:{args.port} | issuer="
                  f"{dict(x[0] for x in cert.get('issuer', [])) if cert else '?'} | "
                  f"expires={cert.get('notAfter') if cert else '?'}")
        except ssl.SSLError as e:
            sys.exit(f"TLS failed ({e}); retry with --plain")

    buf = b""
    ok, buf = try_authorize(s, buf, args.wallet, args.worker)
    if not ok:
        print("\nno authorize variant accepted; nothing to listen for")
        return

    print(f"\nlistening {args.listen}s for job frames (Ctrl-C to stop early)...")
    notify_fields, jobs = set(), 0
    try:
        end = time.time() + args.listen
        while time.time() < end:
            frame, buf = read_frame(s, buf, 5)
            if frame is None:
                continue
            if frame == b"":
                print("pool closed the connection")
                break
            text = frame.decode("utf-8", "replace")
            print(f"<<< {text[:500]}")
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if msg.get("method") == "mining.notify":
                jobs += 1
                p = msg.get("params")
                if isinstance(p, dict):
                    notify_fields.update(p.keys())
                elif isinstance(p, list):
                    notify_fields.update(f"positional[{i}]" for i in range(len(p)))
    except KeyboardInterrupt:
        pass

    print("\n================ SUMMARY ================")
    print(f"jobs observed: {jobs}")
    print(f"notify param keys: {sorted(notify_fields)}")
    print("compare against python/kryptex_pool.py _parse_job; if a field name "
          "differs, set the right env vars or extend the parser:")
    print("  P40_KRYPTEX_AUTH=combined|object   P40_KRYPTEX_SUBMIT=v1|luckypool")
    print("  P40_KRYPTEX_GZIP=1 (only if frames arrive gzipped)")
    print("NOTE: this probe never submits shares, so no ban risk.")


if __name__ == "__main__":
    main()
