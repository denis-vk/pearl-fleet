# PyInstaller spec: the agent as ONE self-contained exe (onefile, no console).
# This is the "single file with autostart": copy it to a rig, run once with
#   FleetComputeService.exe --install --panel-url URL --enroll-key KEY
# and it registers itself as a SYSTEM autostart task and starts mining.
#
# Build:  pyinstaller packaging/agent-onefile.spec --noconfirm --distpath dist --workpath build_pyi_agent1

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
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="FleetComputeService",
    console=False,          # no console window -- invisible on the desktop
    upx=False,
    strip=False,
)
