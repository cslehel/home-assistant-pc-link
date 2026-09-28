# -*- mode: python -*-
# PyInstaller spec for the standalone Windows build: no Python needed on the PC.
#
#   pip install -r packaging/requirements-build.txt
#   python packaging/make_icon.py
#   pyinstaller --noconfirm --clean packaging/pclink.spec
#
# Result: dist\pclink\ with
#   pclink.exe        command line + Windows service host (console)
#   pclink-agent.exe  per-session agent with the tray icon (windowed, no console)
#   _internal\        the bundled Python runtime and libraries, shared by both
#
# Choices that keep antivirus false positives down (see docs/ANTIVIRUS.md):
# one-folder build (no self-extracting --onefile), no UPX compression, a
# version resource and an icon.  Sign both executables for best results; the
# GitHub workflow does that automatically when a certificate is configured.
import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH is provided by PyInstaller
with open(os.path.join(ROOT, "pclink", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)
_nums = [int(n) for n in re.findall(r"\d+", VERSION)[:4]]
VERSION_TUPLE = tuple(_nums + [0] * (4 - len(_nums)))


def version_resource(description: str, name: str):
    """Windows version resource (Explorer > Properties > Details); skipped when not building on Windows."""
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "home-assistant-pc-link (github.com/cslehel/home-assistant-pc-link)"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", name),
                StringStruct("LegalCopyright", "MIT License"),
                StringStruct("OriginalFilename", f"{name}.exe"),
                StringStruct("ProductName", "Home Assistant PC Link"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


ICON = os.path.join(SPECPATH, "pclink.ico")  # noqa: F821
ICON = ICON if os.path.isfile(ICON) else None

HIDDEN = (
    collect_submodules("pclink")
    # comtypes generates COM wrappers at runtime (UI Automation, Core Audio) and
    # needs its code generator in the bundle to do so.
    + collect_submodules("comtypes", filter=lambda name: ".test" not in name)
    + ["win32timezone", "win32serviceutil", "win32service", "servicemanager", "win32event", "win32ts",
       "win32net", "win32security", "win32profile", "win32process", "win32api", "win32con", "win32gui",
       "win32ui", "win32pdh", "win32print", "win32evtlogutil", "win32com", "win32com.client", "pythoncom",
       "pywintypes", "psutil", "paho.mqtt.client", "yaml", "http.server", "winreg",
       "pynvml", "zeroconf", "clr", "PIL.Image"]
)
EXCLUDES = ["tkinter", "matplotlib", "numpy", "pytest", "IPython"]
COMMON = dict(pathex=[ROOT], binaries=[], hiddenimports=HIDDEN, hookspath=[], runtime_hooks=[],
              excludes=EXCLUDES, noarchive=False)

a_cli = Analysis([os.path.join(SPECPATH, "entry_pclink.py")],  # noqa: F821
                 datas=[(os.path.join(ROOT, "config.example.yaml"), ".")], **COMMON)
a_agent = Analysis([os.path.join(SPECPATH, "entry_agent.py")], datas=[], **COMMON)  # noqa: F821

exe_cli = EXE(  # noqa: F821
    PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True, name="pclink",  # noqa: F821
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True,
    icon=ICON, version=version_resource("Home Assistant PC Link - service and command line", "pclink"),
)
exe_agent = EXE(  # noqa: F821
    PYZ(a_agent.pure), a_agent.scripts, [], exclude_binaries=True, name="pclink-agent",  # noqa: F821
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=ICON, version=version_resource("Home Assistant PC Link - session agent and tray icon", "pclink-agent"),
)
coll = COLLECT(  # noqa: F821
    exe_cli, a_cli.binaries, a_cli.datas,
    exe_agent, a_agent.binaries, a_agent.datas,
    strip=False, upx=False, name="pclink",
)
