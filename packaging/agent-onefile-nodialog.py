"""Bootstrap wrapper: catches ALL errors, writes to a local file, never shows UI.

The default PyInstaller GUI bootloader shows an "Unhandled exception" dialog
that we cannot suppress from Python. This file replaces it with a wrapper that
intercepts the real __main__, catches everything, and logs silently.
"""
import os
import sys
import time
import traceback

_LOG = None


def _find_log():
    global _LOG
    if _LOG is not None:
        return _LOG
    try:
        base = os.path.dirname(os.path.abspath(sys.executable))
        logs = os.path.join(base, "logs")
        os.makedirs(logs, exist_ok=True)
        _LOG = os.path.join(logs, "agent.log")
    except Exception:
        _LOG = os.path.join(os.environ.get("TEMP", "."), "fleet_agent.log")
    return _LOG


def _write(msg):
    try:
        with open(_find_log(), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _write_crash(tb_text):
    for p in (_find_log().replace("agent.log", "crash.log"),
              os.path.join(os.environ.get("TEMP", "."), "fleet_crash.log")):
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{tb_text}\n")
            return
        except Exception:
            continue


if __name__ == "__main__":
    _write("=== agent starting ===")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from agent.__main__ import main as _real_main
        _real_main()
    except SystemExit:
        raise
    except BaseException:
        _write_crash(traceback.format_exc())
        _write("=== agent crashed (see crash.log) ===")
        sys.exit(1)
