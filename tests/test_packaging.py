"""Standalone-build support: runtime detection, agent launch command, selftest,
entry scripts and the icon generator (all runnable on any OS)."""
from __future__ import annotations

import os
import struct
import subprocess
import sys

import pytest

from pclink import runtime
from pclink.launcher import agent_command

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, PYTHONPATH=ROOT)
for key in ("PCLINK_SESSION", "PCLINK_AGENT_TOKEN"):
    ENV.pop(key, None)


def run(args, **kw):
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60, **kw)


# -- runtime ----------------------------------------------------------------------------
def test_python_install_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not runtime.is_frozen()
    assert runtime.cli_name() == "python -m pclink"
    assert runtime.service_binary() == (None, None)  # pywin32's pythonservice.exe hosts the service
    assert os.path.isdir(os.path.join(runtime.app_dir(), "pclink"))


def test_standalone_mode(monkeypatch, tmp_path):
    exe = tmp_path / "pclink.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert runtime.is_frozen()
    assert runtime.cli_name() == "pclink.exe"
    assert runtime.app_dir() == str(tmp_path)
    assert runtime.service_binary() == (str(exe), "service")


# -- agent launch command -------------------------------------------------------------------
def test_agent_command_standalone_prefers_agent_exe(tmp_path):
    exe = tmp_path / "pclink.exe"
    exe.write_bytes(b"")
    (tmp_path / runtime.AGENT_EXE).write_bytes(b"")
    cmd, cwd, needs_path = agent_command(frozen=True, executable=str(exe))
    assert cmd == f'"{tmp_path / runtime.AGENT_EXE}"'
    assert cwd == str(tmp_path) and needs_path is False


def test_agent_command_standalone_falls_back_to_cli(tmp_path):
    exe = tmp_path / "pclink.exe"
    exe.write_bytes(b"")
    cmd, cwd, needs_path = agent_command(frozen=True, executable=str(exe))
    assert cmd == f'"{exe}" agent' and needs_path is False


def test_agent_command_python_install(tmp_path):
    python = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    cmd, cwd, needs_path = agent_command(str(python), frozen=False)
    assert cmd == f'"{python}" -X utf8 -m pclink.agent'
    assert cwd == str(tmp_path) and needs_path is True


def test_python_m_pclink_agent_runs():
    """The service starts agents with ``-m pclink.agent``; without the launch
    environment the agent must start and refuse with exit code 2."""
    res = run(["-m", "pclink.agent"])
    assert res.returncode == 2, res.stderr


# -- entry scripts used by PyInstaller -------------------------------------------------------
def test_entry_scripts_use_absolute_imports():
    res = run([os.path.join("packaging", "entry_pclink.py"), "version"])
    assert res.returncode == 0, res.stderr
    import pclink

    assert res.stdout.strip() == pclink.__version__
    res = run([os.path.join("packaging", "entry_agent.py")])
    assert res.returncode == 2, res.stderr


def test_spec_references_existing_entry_scripts():
    with open(os.path.join(ROOT, "packaging", "pclink.spec"), encoding="utf-8") as fh:
        spec = fh.read()
    for name in ("entry_pclink.py", "entry_agent.py"):
        assert name in spec
        assert os.path.isfile(os.path.join(ROOT, "packaging", name))
    assert 'name="pclink"' in spec and 'name="pclink-agent"' in spec
    assert "upx=False" in spec and "console=False" in spec


# -- selftest ---------------------------------------------------------------------------------
def test_selftest_passes_with_core_modules():
    res = run(["-m", "pclink", "selftest", "--require", "psutil,paho-mqtt,pyyaml"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "psutil" in res.stdout and res.stdout.strip().endswith("OK")


def test_selftest_fails_for_missing_required_component():
    res = run(["-m", "pclink", "selftest", "--require", "agent-exe"])  # not a standalone build
    assert res.returncode == 1
    assert "FAILED: agent-exe" in res.stdout


def test_selftest_rejects_unknown_component():
    res = run(["-m", "pclink", "selftest", "--require", "nonsense"])
    assert res.returncode == 2


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows stub")
def test_service_host_command_off_windows():
    res = run(["-m", "pclink", "service"])
    assert res.returncode != 0
    assert "only available on Windows" in (res.stdout + res.stderr)


# -- icon -----------------------------------------------------------------------------------
def test_icon_is_a_valid_multi_size_ico(tmp_path):
    target = tmp_path / "pclink.ico"
    res = run([os.path.join("packaging", "make_icon.py"), str(target)])
    assert res.returncode == 0, res.stderr
    data = target.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1) and count >= 4
    sizes = []
    for i in range(count):
        w, h, _c, _r, planes, bpp, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        size = w or 256
        sizes.append(size)
        assert w == h and planes == 1 and bpp == 32
        assert offset + length <= len(data)
        header_size, dib_w, dib_h = struct.unpack_from("<Iii", data, offset)
        assert header_size == 40 and dib_w == size and dib_h == 2 * size  # DIB height covers colour + mask
    assert 16 in sizes and 32 in sizes and 48 in sizes
