"""Fleet panel client: enroll, heartbeat, bundle download. stdlib urllib only."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request


class PanelError(Exception):
    pass


class PanelClient:
    def __init__(self, base_url, token=None, enroll_key=None, timeout=20):
        self.base = base_url.rstrip("/")
        self.token = token
        self.enroll_key = enroll_key
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing
    def _request(self, path, payload=None, method=None, retries=3, auth=True):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
        if method is None:
            method = "POST" if data is not None else "GET"
        last_err = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                raise PanelError(f"{method} {path} -> HTTP {e.code}: {body}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise PanelError(f"{method} {path} failed after {retries} tries: {last_err}")

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
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
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
