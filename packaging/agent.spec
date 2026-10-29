# PyInstaller spec for RigAgent (stdlib-only, onedir, ~15 MB).
#
# Build from the repo root, on Windows:
#   pyinstaller packaging/agent.spec --noconfirm --distpath dist --workpath build_pyi_agent

import os

ROOT = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join(ROOT, "agent", "__main__.py")],
    pathex=[os.path.join(ROOT, "agent")],
    binaries=[],
    datas=[],
    hiddenimports=["gpu_inventory", "panel_client", "miner_supervisor", "updater",
                   "av_guard"],
    excludes=["torch", "tkinter", "matplotlib", "PIL", "pytest", "IPython",
              "numpy", "scipy", "cupy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rig-agent", console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rig-agent")
