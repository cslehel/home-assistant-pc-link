"""Third review: the security-relevant parts exercised for real - real sockets, real
processes, real Authenticode signatures, real network adapters, real junctions and hard
links - instead of fakes.  Nothing here needs administrator rights; what needs Windows is
skipped elsewhere."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from pclink import codesign, discovery, hardening, ipc
from pclink.api import ApiServer
from pclink.config import ApiConfig

from .test_api import FakeBackend

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="needs Windows")


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback here")


def _elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# -- 1. the API takes its port on both loopback addresses, exclusively ---------------------------------

def _bind(family: int, address: str, port: int, reuse: bool = False) -> bool:
    """Can another socket take ``address:port``?"""
    with socket.socket(family, socket.SOCK_STREAM) as s:
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind((address, port))
            s.listen(1)
            return True
        except OSError:
            return False


def _health(url: str) -> int:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status


def _api(host: str, port: int = 0) -> ApiServer:
    srv = ApiServer(ApiConfig(enabled=True, host=host, port=port, token="t"), FakeBackend())
    srv.start()
    return srv


@windows_only
@needs_ipv6
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_api_holds_both_loopback_addresses_exclusively(host):
    srv = _api(host)
    try:
        port = srv.port
        held = sorted(s.server_address[0] for s in [srv._server] + srv._extra)
        assert held == ["127.0.0.1", "::1"]
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            assert not _bind(family, address, port), address
            assert not _bind(family, address, port, reuse=True), address   # SO_REUSEADDR does not get in either
        assert _health(f"http://127.0.0.1:{port}/api/health") == 200
        assert _health(f"http://[::1]:{port}/api/health") == 200
    finally:
        srv.stop()
    assert _bind(socket.AF_INET, "127.0.0.1", port) and _bind(socket.AF_INET6, "::1", port)   # all released


@windows_only
@needs_ipv6
@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_api_on_a_wildcard_holds_both_wildcards(host):
    srv = _api(host)
    try:
        port = srv.port
        assert sorted(s.server_address[0] for s in [srv._server] + srv._extra) == ["0.0.0.0", "::"]
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1"),
                                (socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            assert not _bind(family, address, port) and not _bind(family, address, port, reuse=True), address
        assert _health(f"http://127.0.0.1:{port}/api/health") == 200
        assert _health(f"http://[::1]:{port}/api/health") == 200
    finally:
        srv.stop()


@windows_only
@needs_ipv6
@pytest.mark.parametrize("reuse", [False, True])
def test_api_does_not_start_next_to_a_squatter_on_ipv6_loopback(reuse):
    squatter = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        if reuse:
            squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        squatter.bind(("::1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=port, token="t"), FakeBackend())
        with pytest.raises(OSError):
            srv.start()
        assert srv._server is None
        assert _bind(socket.AF_INET, "127.0.0.1", port)   # the IPv4 side it had taken is given back
    finally:
        squatter.close()


@windows_only
@needs_ipv6
def test_a_wildcard_squatter_does_not_get_the_loopback_clients():
    # Windows hands a connection to the most specific binding: the API's loopback listeners
    # win over somebody else's wildcard ones on the same port
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv = None
    try:
        s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s4.bind(("0.0.0.0", 0))
        s4.listen(1)
        port = s4.getsockname()[1]
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s6.bind(("::", port))
        s6.listen(1)
        srv = _api("127.0.0.1", port)
        assert _health(f"http://127.0.0.1:{port}/api/health") == 200
        assert _health(f"http://[::1]:{port}/api/health") == 200
        assert _health(f"http://localhost:{port}/api/health") == 200
    finally:
        if srv is not None:
            srv.stop()
        s4.close()
        s6.close()


# -- 2. the agent connection, end to end ------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_launched_agent_and_service_talk_both_ways():
    port = _free_port()
    ipc.remember_launch(4101, "launch-4101")
    reports, messages, seen = [], [], []
    server = ipc.AgentServer("127.0.0.1", port, on_report=lambda sid, r: reports.append((sid, r)),
                             validate=lambda sid, tok: tok == ipc.launched_token(sid),
                             on_message=lambda sid, m: messages.append((sid, m)))
    server.start()
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 4101, "user": "kevin", "pid": os.getpid()},
                             on_message=seen.append, reconnect_seconds=0.05, token="launch-4101")
    client.start()
    try:
        assert client.connected.wait(5) and seen[0]["type"] == "hello_ok"
        assert server.connected_sessions()[4101]["user"] == "kevin"
        assert client.send({"type": "report", "session_id": 999, "ts": time.time(), "domain": "example.com"})
        assert client.send({"type": "notified", "via": "box"})
        assert _wait(lambda: reports and messages)
        assert reports[0][0] == 4101 and reports[0][1].session_id == 4101   # never the session the agent claims
        assert messages[0] == (4101, {"type": "notified", "via": "box"})
        assert server.send(4101, {"type": "run", "command": "notepad.exe"})
        assert _wait(lambda: len(seen) >= 2) and seen[1]["type"] == "run"
    finally:
        client.stop()
        server.stop()


def test_agent_with_a_wrong_token_is_never_served():
    port = _free_port()
    ipc.remember_launch(4102, "right")
    reports, messages, seen, connects = [], [], [], []
    server = ipc.AgentServer("127.0.0.1", port, on_report=lambda *a: reports.append(a),
                             validate=lambda sid, tok: tok == ipc.launched_token(sid),
                             on_message=lambda *a: messages.append(a), on_connect=lambda *a: connects.append(a))
    server.start()
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 4102}, on_message=seen.append,
                             reconnect_seconds=0.05, token="wrong")
    client.start()
    try:
        assert client.rejected.wait(5)   # the service's proof does not match its token: a stale agent leaves
        server.send(4102, {"type": "run", "command": "calc.exe"})
        assert not client.send({"type": "report"})
        time.sleep(0.2)
        assert seen == [] and reports == [] and messages == [] and connects == []
        assert not server.connected_sessions()
    finally:
        client.stop()
        server.stop()


class _Impostor:
    """Holds the agent port and answers with a challenge signed with another token, then orders."""

    def __init__(self, port: int, token: str = "not-the-launch-token"):
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.token = token
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                hello = json.loads(ipc._LineReader(conn).readline(65536))
                nonce = ipc.new_nonce()
                conn.sendall(json.dumps({"type": "challenge", "nonce": nonce,
                                         "proof": ipc.proof(self.token, "service", hello["nonce"], nonce)}).encode() + b"\n")
                conn.sendall(b'{"type":"hello_ok"}\n{"type":"run","command":"calc.exe"}\n')
                time.sleep(0.3)
            except (OSError, ValueError, KeyError):
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def test_agent_gives_up_on_a_listener_signing_with_another_token():
    port = _free_port()
    impostor = _Impostor(port)
    seen = []
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 5}, on_message=seen.append,
                             reconnect_seconds=0.05, token="launch-token")
    client.start()
    try:
        assert client.rejected.wait(10)
        assert impostor.connections == ipc.MAX_BAD_PROOFS
        assert seen == [] and not client.connected.is_set()
    finally:
        client.stop()
        impostor.close()


def test_old_style_hello_with_the_token_is_turned_away():
    port = _free_port()
    ipc.remember_launch(4103, "tok")
    messages = []
    server = ipc.AgentServer("127.0.0.1", port, on_report=lambda *a: None,
                             validate=lambda sid, tok: tok == ipc.launched_token(sid), on_message=lambda *a: messages.append(a))
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b'{"type":"hello","session_id":4103,"token":"tok","pid":1}\n'
                         b'{"type":"notified","via":"box"}\n')
            reader = ipc._LineReader(sock)
            assert json.loads(reader.readline(65536))["type"] == "hello_rejected"
            assert reader.readline(65536) is None   # and the connection is closed
        assert messages == [] and not server.connected_sessions()
    finally:
        server.stop()


_WATCHER = r"""
import sys, threading, time
sys.path.insert(0, sys.argv[2])
from pclink.agent.main import ServiceWatch
gone = threading.Event()
ServiceWatch(int(sys.argv[1]), gone.set, period=0.2).start()
time.sleep(0.4)
print("early" if gone.is_set() else "waiting", flush=True)
print("gone" if gone.wait(20) else "missed", flush=True)
"""


def test_service_watch_notices_its_service_end_from_another_process(tmp_path):
    """The agent's watch on the service, with real creation times: the "service" is older than
    the watching process, as it is in real life."""
    script = tmp_path / "watcher.py"
    script.write_text(_WATCHER, encoding="utf-8")
    service = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5)"])
    # reaped as soon as it ends: on Linux an unreaped child stays a zombie that still has a pid
    # and a creation time (a real service is no child of the test)
    threading.Thread(target=service.wait, daemon=True).start()
    try:
        time.sleep(0.3)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, str(script), str(service.pid), root], capture_output=True, text=True,
                             timeout=60)
        assert out.stdout.split() == ["waiting", "gone"], out.stdout + out.stderr
        assert service.poll() is not None
    finally:
        if service.poll() is None:
            service.kill()


# -- 3. code signatures read with Windows' own tools -----------------------------------------------------

def _signed_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


@windows_only
def test_real_signatures_and_their_issuing_ca(tmp_path):
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    catalog_signed = os.path.join(system32, "notepad.exe")
    embedded = str(tmp_path / "embedded.exe")
    shutil.copyfile(_signed_exe(), embedded)
    broken = str(tmp_path / "broken.exe")
    data = bytearray(open(embedded, "rb").read())
    data[0x400] ^= 0xFF   # inside the code, not the signature
    open(broken, "wb").write(bytes(data))
    script = str(tmp_path / "unsigned.ps1")
    open(script, "w", encoding="utf-8").write("Write-Output 1\n")
    found = codesign._by_path(codesign._run([catalog_signed, embedded, broken, script]))
    cat, emb, bad, plain = (found[os.path.normcase(p)] for p in (catalog_signed, embedded, broken, script))

    for sig in (cat, emb):
        assert sig.valid and "Microsoft" in sig.subject and sig.signer
        assert len(sig.thumbprint) == 40 and len(sig.root) == 40 and len(sig.ca) == 40
        assert sig.ca_subject.startswith("CN=") and sig.issuer == sig.ca_subject
        assert len(sig.ca_key) == 64 and int(sig.ca_key, 16) >= 0
    assert bad.status == "invalid" and bad.thumbprint == emb.thumbprint   # the signer is still read, the hash is wrong
    assert plain.status == "unsigned" and not plain.thumbprint and not plain.ca

    # the same publisher: the same certificate; or a renewal from the same CA and root
    assert codesign.same_publisher(emb, cat) == (emb.thumbprint == cat.thumbprint or
                                                 (emb.subject == cat.subject and emb.root == cat.root))
    renewed = codesign.Signature("valid", "AB" * 20, emb.subject, root=emb.root, ca="CD" * 20,
                                 ca_subject=emb.ca_subject, ca_key=emb.ca_key)
    assert codesign.same_publisher(renewed, emb)                                    # re-issued CA certificate
    assert not codesign.same_publisher(codesign.Signature("valid", "AB" * 20, emb.subject, root=emb.root,
                                                          ca="CD" * 20, ca_subject=emb.ca_subject, ca_key="00" * 32), emb)
    assert not codesign.same_publisher(codesign.Signature("valid", "AB" * 20, emb.subject, root="EF" * 20,
                                                          ca=emb.ca, ca_subject=emb.ca_subject, ca_key=emb.ca_key), emb)

    # the recorded signer is never lost to a broken or unsigned executable; a valid one updates it
    assert codesign.anchor(emb, bad) == (emb, False) and codesign.anchor(emb, plain) == (emb, False)
    assert codesign.anchor(None, emb) == (emb, True) and codesign.anchor(emb, emb) == (emb, False)
    assert codesign.anchor(None, bad)[0].status == "unknown"
    record = str(tmp_path / "signer.json")
    assert codesign.save_record(record, emb) and codesign.load_record(record) == codesign.Signature(
        "valid", emb.thumbprint, emb.subject, "", emb.issuer, emb.root, emb.ca, emb.ca_subject, emb.ca_key)


@windows_only
def test_signature_of_a_file_in_a_folder_with_non_ascii_characters(tmp_path):
    folder = tmp_path / "Szőke Ádám [1]"
    folder.mkdir()
    exe = str(folder / "pclink é.exe")
    shutil.copyfile(_signed_exe(), exe)
    sig = codesign.read_signature(exe)
    assert sig.valid and "Microsoft" in sig.subject, sig.detail   # was "unknown": PowerShell answered in the OEM code page


@windows_only
def test_package_with_a_real_catalog(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for name in codesign.EXECUTABLES:
        shutil.copyfile(_signed_exe(), pkg / name)
    (pkg / codesign.INSTALLER).write_text("Write-Output 1\n", encoding="utf-8")
    (pkg / "python3.dll").write_bytes(b"runtime")
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                              "powershell.exe")
    subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                    f"New-FileCatalog -Path '{pkg}' -CatalogFilePath '{pkg / codesign.CATALOG}' -CatalogVersion 2.0 | Out-Null"],
                   check=True, capture_output=True, timeout=120)
    found = codesign.read_package(str(pkg))
    assert found.error is None and found.catalog_matches is True
    assert {n: s.status for n, s in found.files.items()} == {
        "pclink.exe": "valid", "pclink-agent.exe": "valid", "install.ps1": "unsigned", "pclink.cat": "unsigned"}
    assert codesign.judge(found)[0]                         # an unsigned installation takes it ...
    assert not codesign.judge(found, require=True)[0]       # ... unless a signature is required
    found.installed = found.files["pclink.exe"]
    assert "install.ps1 is not signed" in codesign.judge(found)[1]   # a signed one wants everything signed
    (pkg / "python3.dll").write_bytes(b"changed")
    assert codesign.read_package(str(pkg)).catalog_matches is False


# -- 4. which addresses are announced on the LAN ------------------------------------------------------------

def test_virtual_adapters_are_also_known_by_their_description():
    va = discovery.virtual_adapter
    assert va("Local Area Connection", "TAP-Windows Adapter V9 for OpenVPN Connect", 6)
    assert va("OpenVPN Connect DCO Adapter", "OpenVPN Data Channel Offload", 53)
    assert va("WARP", "Cloudflare WARP Interface Tunnel", 53)
    assert va("Mullvad", "Wintun Userspace Tunnel", 53)
    assert va("Local Area Connection* 1", "Microsoft Wi-Fi Direct Virtual Adapter", 71)
    assert va("Ethernet 3", "Some VPN driver", 53)   # IF_TYPE_PROP_VIRTUAL
    assert not va("Ethernet", "Intel(R) Ethernet Controller (3) I225-V", 6)
    assert not va("Wi-Fi", "Intel(R) Wi-Fi 6E AX211 160MHz", 71)
    assert not va("vEthernet (External LAN)", "Hyper-V Virtual Ethernet Adapter", 6)   # an external switch is the LAN
    assert va("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter", 6)


def test_a_vpn_named_like_a_lan_adapter_is_not_announced(monkeypatch):
    import psutil

    addr = lambda ip: type("A", (), {"family": socket.AF_INET, "address": ip})()   # noqa: E731
    stat = type("S", (), {"isup": True})()
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"Local Area Connection": [addr("10.0.10.2")],
                                                         "Ethernet": [addr("192.168.1.55")]})
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Local Area Connection": stat, "Ethernet": stat})
    monkeypatch.setattr(discovery, "_adapter_details", lambda: {
        "Local Area Connection": ("TAP-Windows Adapter V9 for OpenVPN Connect", 6),
        "Ethernet": ("Intel(R) Ethernet Controller", 6)})
    assert discovery.lan_addresses("0.0.0.0") == ["192.168.1.55"]
    # with the VPN alone, it is still announced (better than nothing)
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"Local Area Connection": [addr("10.0.10.2")]})
    assert discovery.lan_addresses("0.0.0.0") == ["10.0.10.2"]


@windows_only
def test_real_adapter_details_match_psutil_names():
    import psutil

    details = discovery._adapter_details()
    assert details, "GetAdaptersAddresses gave nothing"
    loopback = [name for name, (desc, if_type) in details.items() if if_type == 24]
    assert loopback and all(discovery.virtual_adapter(name, *details[name]) for name in loopback)
    with_ipv4 = [name for name, addrs in psutil.net_if_addrs().items() if any(a.family == socket.AF_INET for a in addrs)]
    assert set(with_ipv4) <= set(details), set(with_ipv4) - set(details)
    for address in discovery.lan_addresses("0.0.0.0"):
        assert not address.startswith(("127.", "169.254."))


# -- 5. the data folder: links, hard links and what a user left there ---------------------------------------

def _mklink(kind: str, link: str, target: str) -> None:
    subprocess.run(["cmd", "/c", "mklink", kind, link, target], check=True, capture_output=True, timeout=30)


@pytest.fixture()
def planted(tmp_path):
    """A data folder (owned by the current user) holding a junction and a hard link to things outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "config.yaml").write_text("device: {}\n", encoding="utf-8")
    (data / "sub" / "b.txt").write_text("b", encoding="utf-8")
    _mklink("/J", str(data / "junc"), str(outside))
    _mklink("/H", str(data / "hard.txt"), str(outside / "secret.txt"))
    return data, outside


@windows_only
def test_links_and_foreign_entries_are_found_and_put_aside(planted):
    data, outside = planted
    path = str(data)
    me = hardening._own_sid()
    assert me and hardening._owner_sid(path) == me
    assert hardening.is_restricted(path) is False                     # a folder a user made is never "restricted"
    assert hardening._is_reparse_point(str(data / "junc")) and hardening._link_count(str(data / "hard.txt")) == 2
    assert hardening._link_count(str(data / "config.yaml")) == 1

    foreign = hardening.foreign_owned(path)
    assert path in foreign and str(data / "junc") in foreign and str(data / "config.yaml") in foreign
    assert str(data / "junc" / "secret.txt") not in foreign            # the junction is not looked into

    # trusting this account (as the installer, run by an administrator, does): only the links
    trusted = hardening._TRUSTED_OWNERS | {me}
    assert hardening._strangers(path, trusted) == [str(data / "junc"), str(data / "hard.txt")]
    # trusting only the built-in accounts: everything this user wrote as well
    strangers = hardening._strangers(path, set(hardening._TRUSTED_OWNERS))
    assert strangers == [str(data / "junc"), str(data / "sub"), str(data / "config.yaml"), str(data / "hard.txt")]

    where = hardening._quarantine(path, strangers)
    assert os.path.basename(where).startswith(hardening.QUARANTINE_PREFIX)
    assert os.listdir(path) == [os.path.basename(where)]
    assert hardening._is_reparse_point(os.path.join(where, "junc"))          # the link moved, not what it points to
    assert os.path.isfile(os.path.join(where, "sub", "b.txt"))
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret" and os.listdir(outside) == ["secret.txt"]
    assert hardening._strangers(path, set(hardening._TRUSTED_OWNERS)) == []   # the quarantine is left alone
    assert hardening._quarantine(path, []) != where                           # a second one gets its own name


@windows_only
def test_a_junction_is_never_secured_or_used(planted):
    data, outside = planted
    junction = str(data / "junc")
    assert hardening.data_dir_problem(junction).startswith(junction)
    assert hardening.secure_data_dir(junction) is False
    with pytest.raises(hardening.DataDirError):
        hardening.ensure_data_dir(junction)
    assert os.listdir(outside) == ["secret.txt"]


@windows_only
@pytest.mark.skipif(_elevated(), reason="elevated: the folder would really be taken over")
def test_without_administrator_rights_the_folder_is_not_reported_safe(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("x", encoding="utf-8")
    try:
        assert hardening.secure_data_dir(str(data)) is False   # ownership cannot be given to Administrators
        assert hardening.is_restricted(str(data)) is False
    finally:
        # the owner may always change the permissions back (so pytest can clean up)
        subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe"), str(data),
                        "/grant", f"{os.environ.get('USERNAME', '')}:(OI)(CI)F", "/T", "/Q"], capture_output=True, timeout=60)
