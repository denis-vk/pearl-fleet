"""Run the panel server:  python -m panel [--host 0.0.0.0] [--port 8000]

Requires: pip install fastapi uvicorn   (see docs/FLEET.md)
Env: PANEL_ADMIN_PASSWORD (initial admin password; printed if unset)
     PANEL_DB (sqlite path, default panel.db)
"""
import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="Pearl fleet panel")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("panel.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
