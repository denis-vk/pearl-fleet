"""Kryptex Pearl pool client (TLS stratum) for the fleet build.

Interface-compatible with pool_common.LuckyPool (connect / next_job /
check_newer_job / submit), so miner_capi.run_single_gpu uses it unchanged.

Dialect: newline-delimited JSON-RPC shaped after the open "Pearl Stratum
Protocol V1" spec (github.com/nushypool/pearl_stratum_protocol_v1), which is how
krig connects (`krig-miner --url stratum+ssl://host:port --user WALLET/WORKER`):

    authorize: {"id":1,"method":"mining.authorize","params":["<wallet>.<worker>"]}
    notify:    {"id":null,"method":"mining.notify","params":{"job_id","header",
                "target","height","difficulty"}}
    submit:    {"id":N,"method":"mining.submit","params":{"job_id","plain_proof"}}

Kryptex does not publish its exact wire format, so packaging is configurable via
env vars (use tools/probe_kryptex.py against the live pool to pick values):
    P40_KRYPTEX_TLS=0                plain TCP instead of TLS (debugging)
    P40_KRYPTEX_GZIP=1               gzip outbound submit bodies
    P40_KRYPTEX_AUTH=combined|object authorize params style (default combined)
    P40_KRYPTEX_SUBMIT=v1|luckypool  submit params style (default v1)

Inbound frames that start with the gzip magic (0x1f 0x8b) are decompressed
transparently, so a gzip-enabled pool works regardless of the outbound setting.
The share target may arrive either as 64-hex (32-byte big-endian) or as compact
nbits; both are handled (NbitsToTarget, like krig).
"""
from __future__ import annotations

import gzip
import json
import os
import socket
import ssl
import threading
import time

GZIP_MAGIC = b"\x1f\x8b"


def nbits_to_target(nbits: int) -> int:
    """Bitcoin-style compact nbits (uint32) -> 256-bit integer target."""
    size = nbits >> 24
    word = nbits & 0x007FFFFF
    if size <= 3:
        return word >> (8 * (3 - size))
    return word << (8 * (size - 3))


def _maybe_gunzip(frame: bytes) -> bytes:
    if frame[:2] == GZIP_MAGIC:
        try:
            return gzip.decompress(frame)
        except OSError:
            return frame
    return frame


class KryptexPool:
    """TLS stratum client for the Kryptex Pearl pool (LuckyPool-compatible API)."""

    def __init__(self, host, port, wallet, worker):
        self.host, self.port = host, port
        self.wallet, self.worker = wallet, worker
        self.s = None
        self.buf = b""
        self.difficulty = None
        # Serializes socket access between the mining loop (check_newer_job)
        # and the background proof-submitter thread, exactly like LuckyPool.
        self.io_lock = threading.Lock()

    # ------------------------------------------------------------------ wire
    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=30)
        if os.environ.get("P40_KRYPTEX_TLS", "1").lower() not in ("0", "false", "no"):
            ctx = ssl.create_default_context()
            self.s = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.s = raw
        self.s.settimeout(60)
        style = os.environ.get("P40_KRYPTEX_AUTH", "combined")
        if style == "object":
            params = {"wallet": self.wallet, "worker": self.worker}
        else:  # "combined": Pearl Stratum V1 style "address.worker"
            params = [f"{self.wallet}.{self.worker}" if self.worker else self.wallet]
        self._send("mining.authorize", params)

    def _send(self, method, params, mid=1):
        with self.io_lock:
            self._send_unlocked(method, params, mid)

    def _send_unlocked(self, method, params, mid):
        """Send one JSON-RPC frame; caller must hold io_lock (or be connecting)."""
        payload = (json.dumps({"id": mid, "method": method, "params": params}) + "\n").encode()
        if method == "mining.submit" and os.environ.get("P40_KRYPTEX_GZIP", "0") == "1":
            # krig's traffic saver: the multi-hundred-KB base64 proof compresses
            # ~80%. Framing guess: gzipped message body + newline (probe first!).
            payload = gzip.compress(payload.rstrip(b"\n")) + b"\n"
        self.s.sendall(payload)

    def _readline(self, timeout):
        """Next complete frame (bytes before a newline, gunzipped if needed)."""
        self.s.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                d = self.s.recv(65536)
            except (socket.timeout, BlockingIOError):
                return None
            if not d:
                return None
            self.buf += d
        line, self.buf = self.buf.split(b"\n", 1)
        return _maybe_gunzip(line)

    def _drain_socket(self):
        """Pull everything currently readable into self.buf (best effort)."""
        while True:
            self.s.settimeout(0)
            try:
                d = self.s.recv(65536)
            except (BlockingIOError, ssl.SSLWantReadError, TimeoutError, OSError):
                return
            if not d:
                raise ConnectionError("pool closed the connection")
            self.buf += d

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _parse_job(msg):
        """notify params (object or positional) -> (header, target_int, job_id)."""
        p = msg.get("params")
        if isinstance(p, dict):
            job_id = p.get("job_id")
            header = p.get("header")
            target = p.get("target")
            nbits = p.get("nbits")
        elif isinstance(p, list) and len(p) >= 3:
            job_id, header, target = p[0], p[1], p[2]
            nbits = p[3] if len(p) > 3 else None
        else:
            return None
        if not (job_id and header):
            return None
        if target:
            tgt = int(target, 16) if isinstance(target, str) else int(target)
        elif nbits:
            tgt = nbits_to_target(int(nbits, 16) if isinstance(nbits, str) else nbits)
        else:
            return None
        return bytes.fromhex(header), tgt, job_id

    def next_job(self, timeout=70):
        end = time.time() + timeout
        while time.time() < end:
            line = self._readline(timeout=max(end - time.time(), 0.5))
            if line is None:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("method") == "mining.notify":
                job = self._parse_job(msg)
                if job:
                    if isinstance(msg.get("params"), dict) and msg["params"].get("difficulty"):
                        self.difficulty = msg["params"]["difficulty"]
                    return job
            elif msg.get("method") == "mining.set_difficulty":
                self.difficulty = msg.get("params")
        return None

    def check_newer_job(self, current_job_id):
        if not self.io_lock.acquire(blocking=False):
            return None
        try:
            self._drain_socket()
            newer = None
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = _maybe_gunzip(line)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("method") == "mining.notify":
                    job = self._parse_job(msg)
                    if job and job[2] != current_job_id:
                        newer = job
                elif msg.get("method") == "mining.set_difficulty":
                    self.difficulty = msg.get("params")
            return newer
        finally:
            self.io_lock.release()

    # --------------------------------------------------------------- submits
    def submit(self, job_id, plain_proof_b64):
        style = os.environ.get("P40_KRYPTEX_SUBMIT", "v1")
        if style == "luckypool":
            params = {"wallet": self.wallet, "worker": self.worker,
                      "job_id": job_id, "plain_proof": plain_proof_b64}
        else:  # "v1": Pearl Stratum V1 shape
            params = {"job_id": job_id, "plain_proof": plain_proof_b64}
        with self.io_lock:
            self._send_unlocked("mining.submit", params, mid=99)
            for _ in range(40):
                line = self._readline(timeout=10)
                if line is None:
                    return None
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 99:
                    return msg
                if msg.get("method") == "mining.notify":
                    # park the job for the mining loop's next poll
                    self.buf = line + b"\n" + self.buf
                    return {"pending_job": True}
        return None

    def _send_unlocked(self, method, params, mid):
        """_send without taking io_lock (caller already holds it)."""
        payload = (json.dumps({"id": mid, "method": method, "params": params}) + "\n").encode()
        if method == "mining.submit" and os.environ.get("P40_KRYPTEX_GZIP", "0") == "1":
            payload = gzip.compress(payload.rstrip(b"\n")) + b"\n"
        self.s.sendall(payload)
