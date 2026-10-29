# Fleet API contract (panel <-> rig agent)

Version 1. All bodies are JSON unless noted. The panel serves everything under
one origin; rigs authenticate with `Authorization: Bearer <token>`, the admin
UI with a `panel_session` cookie.

## 1. Enrollment — `POST /api/rig/enroll`

Body: `{enroll_key, name, machine_id, os, agent_version}`.
Response: `{rig_id, token}` — store the token in `agent.state.json`; it never
changes for a given `machine_id` (re-enrollment returns the same pair).

## 2. Heartbeat — `POST /api/rig/heartbeat`

Sent every `heartbeat_sec` (default 30). Body (agent -> panel):

```json
{
  "name": "rig-01",
  "machine_id": "SMBIOS-UUID",
  "os": "Windows 11",
  "agent_version": "1.0.0",
  "engine": "nvidia",
  "gpus": [
    {"index": 0, "vendor": "nvidia", "model": "NVIDIA GeForce RTX 4070",
     "vram_mb": 8192, "driver": "566.36", "compute_cap": "8.9",
     "temp_c": 64, "fan_pct": 55, "power_w": 180, "util_pct": 97}
  ],
  "miner": {"version": "1.5.fleet-3", "state": "running", "pid": 1234,
            "started_ts": 1730000000.0, "restarts": 2},
  "hashrate_ths": {"combined": 24.6, "per_gpu": {"0": 12.1, "1": 12.5}},
  "shares": {"accepted_user": 5, "accepted_dev": 1, "rejected": 0},
  "intensity_pct": 80
}
```

`miner.state`: `stopped | running | crashed | updating`.
`engine`: `nvidia` (sm_61+ present) or `none` (report-only machine).

Response (panel -> agent), the rig's current config:

```json
{"config": {
  "intensity_pct": 80,
  "wallet": "prl1...",
  "worker": "rig-01",
  "target_version": "1.5.fleet-4",
  "restart_seq": 3,
  "miner_enabled": true
}}
```

Agent semantics:
- `intensity_pct` changed -> write `run/intensity.txt` inside the current
  bundle; the miner picks it up live at the next batch flush (no restart).
- `wallet`/`worker`/`restart_seq` changed -> restart the miner process.
- `miner_enabled: false` -> stop the miner, keep heartbeating.
- `target_version` != running version -> download + verify + swap + restart.

## 3. Bundles

- `GET /api/rig/bundle/{version}/manifest` -> `{version, sha256, size}`
- `GET /api/rig/bundle/{version}` -> the zip (streamed)

Bundle zip layout (v2, multi-engine):

```
manifest.json              see fields below
<p40-miner | krig | ...>/  the engine's files
```

manifest.json fields:
- `version` — bundle version string
- `entry` — relative path to the miner executable inside the bundle
- `engine` — `"p40"` (our open NVIDIA build) or an external engine id
  (`"krig"` for AMD rigs, `"blockz"` for Intel Arc/CPU rigs, ...)
- `args` — optional CLI template with `{wallet}` / `{worker}` / `{pool}`
  placeholders. Absent (p40 default) -> the agent passes
  `--wallet <w> --worker <n>`. krig example:
  `["--url", "{pool}", "--user", "{wallet}/{worker}"]`
- `intensity.mode` — `"file"` (p40: live duty cycle via `run/intensity.txt`)
  or `"process_duty"` (external engines: the agent suspend/resumes the whole
  process; the default for non-p40 engines)

Engine routing is per-rig through the panel: upload a p40 bundle and target it
at NVIDIA rigs, a krig bundle at AMD rigs, a blockz bundle at Intel rigs — the
same update / restart / telemetry / intensity pipeline covers all of them.

The agent verifies the downloaded zip against the manifest `sha256` before
touching the running miner, extracts to `bundles/<version>/`, then repoints
the `current` junction and restarts. Old versions are kept for rollback.

## 4. Admin (cookie-auth)

- `POST /api/admin/login` `{password}`
- `GET  /api/admin/state` -> `{rigs: [...], bundles: [...], enroll_key}`
- `POST /api/admin/rigs/{id}/config` — any subset of
  `{intensity_pct, wallet, worker, miner_enabled, target_version}`
- `POST /api/admin/rigs/{id}/restart` — bumps `restart_seq`
- `PUT  /api/admin/bundles/{version}` — raw zip body (not multipart)
- `POST /api/admin/bundles/{version}/deploy` — set `target_version` on all rigs
