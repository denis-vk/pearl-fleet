"""Fleet panel client: enroll, heartbeat, bundle download. stdlib urllib only."""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request


def _ssl_context():
    """HTTPS context that works inside a PyInstaller onefile bundle.

    Frozen Python often cannot find its CA bundle (_MEIPASS/cacert.pem missing),
    which makes urlopen raise FileNotFoundError. Prefer certifi when it is
    bundled; otherwise fall back to the system store, then to unverified.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        return ctx
    except Exception:
        return ssl._create_unverified_context()


_CTX = None


def _urlopen(req, timeout):
    """urlopen with a CA-safe context; retries unverified if the bundle is
    broken (the panel is ours and requests carry a bearer token anyway)."""
    global _CTX
    if _CTX is None:
        _CTX = _ssl_context()
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX)
    except (FileNotFoundError, ssl.SSLError, OSError) as e:
        if "certificate" in str(e).lower() or isinstance(e, FileNotFoundError):
            _CTX = ssl._create_unverified_context()
            return urllib.request.urlopen(req, timeout=timeout, context=_CTX)
        raise


class PanelError(Exception):
    pass


class PanelClient:
    def __init__(self, base_url, token=None, enroll_key=None, timeout=20, fallbacks=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.enroll_key = enroll_key
        self.timeout = timeout
        # alternate panel addresses (e.g. domain first, raw IP as a fallback):
        # if the primary is unreachable the agent switches for good.
        self.fallbacks = [f.rstrip("/") for f in (fallbacks or [])]
        self._dead = set()

    # ------------------------------------------------------------- plumbing
    def _request(self, path, payload=None, method=None, retries=3, auth=True):
        order = [self.base] + [b for b in self.fallbacks
                               if b != self.base and b not in self._dead]
        last_err = None
        for base in order:
            url = base + path
            headers = {"Content-Type": "application/json"}
            if auth and self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            data = None
            if payload is not None:
                data = json.dumps(payload).encode()
            if method is None:
                method = "POST" if data is not None else "GET"
            try:
                for attempt in range(retries):
                    req = urllib.request.Request(url, data=data, headers=headers,
                                                 method=method)
                    try:
                        with _urlopen(req, timeout=self.timeout) as r:
                            if base != self.base:
                                # primary is dead -> stick with this one
                                self.base, base = base, base
                            return json.loads(r.read().decode() or "{}")
                    except urllib.error.HTTPError as e:
                        body = e.read().decode("utf-8", "replace")[:300]
                        raise PanelError(f"{method} {path} -> HTTP {e.code}: {body}")
                    except (urllib.error.URLError, TimeoutError, OSError) as e:
                        last_err = e
                        time.sleep(1.5 * (attempt + 1))
            except PanelError:
                raise
            # transport failure on this base -> try the next address
            self._dead.add(base)
            continue
        raise PanelError(f"{method} {path} failed on all panel addresses: {last_err}")

    # ------------------------------------------------------------------ api
    def enroll(self, machine_payload):
        """Exchange the enrollment key for a rig_id + token (first run only)."""
        resp = self._request("/api/rig/enroll",
                             {"enroll_key": self.enroll_key, **machine_payload})
        if not resp.get("rig_id") or not resp.get("token"):
            raise PanelError(f"enroll response missing rig_id/token: {resp}")
        self.token = resp["token"]
        return resp

    def heartbeat(self, payload):
        """POST telemetry; the response carries the rig's current config."""
        return self._request("/api/rig/heartbeat", payload)

    def bundle_manifest(self, version):
        return self._request(f"/api/rig/bundle/{version}/manifest")

    # ------------------------------------------------------------- download
    def download_bundle(self, version, dest_path, expected_sha256=None, chunk=1 << 20):
        """Stream a bundle zip to dest_path, verifying its sha256. True on success."""
        url = f"{self.base}/api/rig/bundle/{version}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        h = hashlib.sha256()
        tmp = dest_path + ".part"
        try:
            with _urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    block = r.read(chunk)
                    if not block:
                        break
                    f.write(block)
                    h.update(block)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise PanelError(f"bundle download failed: {e}")
        got = h.hexdigest()
        if expected_sha256 and got.lower() != expected_sha256.lower():
            os.remove(tmp)
            raise PanelError(f"bundle sha256 mismatch: got {got}, want {expected_sha256}")
        os.replace(tmp, dest_path)
        return True
