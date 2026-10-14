"""Second review: the agent, its connection to the service, discovery and the slow readers."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

from pclink import ipc
from pclink.agent import main as agent_main


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Impostor:
    """Something else holding the agent port: it answers like the service but does not know the token."""

    def __init__(self, port: int, answer: str = "forge"):
        self.heard: list[dict] = []
        self.answer = answer
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                reader = ipc._LineReader(conn)
                hello = json.loads(reader.readline(65536))
                self.heard.append(hello)
                if self.answer == "forge":   # a challenge with a made-up proof, then orders
                    conn.sendall(json.dumps({"type": "challenge", "nonce": ipc.new_nonce(), "proof": "0" * 64}).encode() + b"\n")
                conn.sendall(b'{"type":"hello_ok"}\n{"type":"run","command":"calc.exe"}\n')
                time.sleep(0.5)
            except (OSError, ValueError):
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


# -- 1. the agent and the service prove the launch token to each other ---------------------------------

def test_agent_obeys_nothing_from_a_listener_that_cannot_prove_the_token():
    port = free_port()
    impostor = Impostor(port)
    seen = []
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 3, "user": "dad", "pid": 1},
                             on_message=seen.append, reconnect_seconds=0.05, token="s3cret-token")
    client.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not client.rejected.is_set():
            time.sleep(0.02)
        assert client.rejected.is_set()                     # a stale agent gives up after a few tries
        assert seen == [] and not client.connected.is_set()   # never a "run"
        assert impostor.heard and all("token" not in h for h in impostor.heard)
        assert all("s3cret-token" not in json.dumps(h) for h in impostor.heard)   # the token is never sent
        assert all(ipc.good_nonce(h["nonce"]) for h in impostor.heard)
    finally:
        client.stop()
        impostor.close()


def test_hello_ok_without_a_challenge_is_not_trusted():
    port = free_port()
    impostor = Impostor(port, answer="skip")
    seen = []
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 3}, on_message=seen.append,
                             reconnect_seconds=0.05, token="t")
    client.start()
    try:
        time.sleep(0.6)
        assert seen == [] and not client.connected.is_set() and not client.rejected.is_set()   # tries again
        assert len(impostor.heard) >= 2
    finally:
        client.stop()
        impostor.close()


def _server(port, tokens):
    server = ipc.AgentServer("127.0.0.1", port, on_report=lambda *a: None,
                             validate=lambda sid, tok: tokens.get(sid) == tok, secret=tokens.get)
    server.start()
    return server


def test_server_rejects_old_agents_and_wrong_proofs_and_replays():
    port = free_port()
    server = _server(port, {4: "good"})
    try:
        # an agent of the previous version sends its token in clear: it is told to leave
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b'{"type":"hello","session_id":4,"token":"good","pid":1}\n')
            assert json.loads(ipc._LineReader(sock).readline(65536))["type"] == "hello_rejected"
        # a proof made with another token
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            reader = ipc._LineReader(sock)
            nonce = ipc.new_nonce()
            sock.sendall(json.dumps({"type": "hello", "session_id": 4, "nonce": nonce}).encode() + b"\n")
            challenge = json.loads(reader.readline(65536))
            assert challenge["type"] == "challenge"
            assert ipc.proof_matches("good", "service", nonce, challenge["nonce"], challenge["proof"])
            sock.sendall(json.dumps({"type": "auth", "proof": ipc.proof("bad", "agent", challenge["nonce"], nonce)}).encode() + b"\n")
            assert json.loads(reader.readline(65536))["type"] == "hello_rejected"
        # the service's own proof played back as the agent's (reflection) does not pass
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            reader = ipc._LineReader(sock)
            nonce = ipc.new_nonce()
            sock.sendall(json.dumps({"type": "hello", "session_id": 4, "nonce": nonce}).encode() + b"\n")
            challenge = json.loads(reader.readline(65536))
            sock.sendall(json.dumps({"type": "auth", "proof": challenge["proof"]}).encode() + b"\n")
            assert json.loads(reader.readline(65536))["type"] == "hello_rejected"
        assert not server.connected_sessions()
        # a session the service launched nothing for
        client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 9}, on_message=lambda m: None,
                                 reconnect_seconds=0.05, token="good")
        client.start()
        assert client.rejected.wait(5)
        client.stop()
    finally:
        server.stop()


def test_real_agent_connects_and_the_launcher_registry_feeds_the_server():
    port = free_port()
    ipc.remember_launch(77, "launch-token")
    server = ipc.AgentServer("127.0.0.1", port, on_report=lambda *a: None,
                             validate=lambda sid, tok: sid == 77 and tok == "launch-token")   # no secret: the registry
    server.start()
    seen = []
    client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 77, "user": "kevin", "token": "launch-token"},
                             on_message=seen.append, reconnect_seconds=0.05)
    client.start()
    try:
        assert client.connected.wait(5)
        assert "token" not in client.hello and seen[0]["type"] == "hello_ok"
        assert server.send(77, {"type": "status", "text": "x"})
        deadline = time.time() + 5
        while time.time() < deadline and len(seen) < 2:
            time.sleep(0.02)
        assert seen[1]["type"] == "status"
    finally:
        client.stop()
        server.stop()


def test_proofs_are_bound_to_role_and_nonces():
    a, b = ipc.new_nonce(), ipc.new_nonce()
    assert ipc.good_nonce(a) and not ipc.good_nonce("short") and not ipc.good_nonce(None) and not ipc.good_nonce("Z" * 32)
    p = ipc.proof("tok", "service", a, b)
    assert ipc.proof_matches("tok", "service", a, b, p)
    assert not ipc.proof_matches("tok", "agent", a, b, p) and not ipc.proof_matches("tok", "service", b, a, p)
    assert not ipc.proof_matches("other", "service", a, b, p) and not ipc.proof_matches("", "service", a, b, p)
    assert not ipc.proof_matches("tok", "service", a, b, None)


# -- 2. the agent leaves when its service is gone ------------------------------------------------------

def test_service_gone_rules():
    assert agent_main.service_gone(False, None, None, None)
    assert not agent_main.service_gone(True, None, None, None)                 # there, creation time unknown
    assert not agent_main.service_gone(True, 100.0, 100.0, 200.0)
    assert agent_main.service_gone(True, 150.0, 100.0, 200.0)                  # another process has the pid now
    assert agent_main.service_gone(True, 300.0, None, 200.0)                   # created after the agent: not its service


def test_service_watch_notices_the_end_of_the_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])
    gone = threading.Event()
    watch = agent_main.ServiceWatch(proc.pid, gone.set, period=0.1)
    watch.own_created = lambda: None   # (the real service is always older than its agent; this child is not)
    watch.start()
    try:
        time.sleep(0.3)
        assert not gone.is_set()
        proc.wait(10)
        assert gone.wait(10)
    finally:
        watch.stop()
        if proc.poll() is None:
            proc.kill()


def test_agent_watches_the_pid_the_launcher_passes(monkeypatch):
    import pclink.launcher as launcher_src

    assert "PCLINK_SERVICE_PID" in open(launcher_src.__file__, encoding="utf-8").read()
    assert agent_main.as_int("4242") == 4242 and agent_main.as_int("nonsense") == 0
    assert agent_main.as_int(None, -1) == -1 and agent_main.as_int(True, 5) == 5 and agent_main.as_int(float("nan")) == 0


# -- 3 / 4 / 6. requests from the service ----------------------------------------------------------------

def _agent():
    import queue

    agent = agent_main.Agent.__new__(agent_main.Agent)
    agent.sent = []
    agent.client = types.SimpleNamespace(connected=threading.Event(), send=lambda m: agent.sent.append(m) or True)
    agent._stop = threading.Event()
    agent._shot_lock = threading.Lock()
    agent._commands = queue.Queue(maxsize=agent_main.MAX_QUEUED_COMMANDS)
    agent.tray = None
    agent._tray_connected, agent._last_status, agent._status_text = False, None, ""
    agent._last_signature, agent._last_report = ("old",), 123.0
    return agent


def test_requests_run_on_the_command_thread_not_the_reader(monkeypatch):
    agent = _agent()
    started, release = threading.Event(), threading.Event()
    threads = []

    def slow_close(pid, name, created):
        threads.append(threading.current_thread().name)
        started.set()
        release.wait(5)
        return "terminated 1"

    monkeypatch.setattr(agent_main, "close_app", slow_close)
    worker = threading.Thread(target=agent._command_loop, name="commands", daemon=True)
    worker.start()
    t0 = time.monotonic()
    agent._on_message({"type": "close", "pid": 7, "name": "game.exe"})
    assert time.monotonic() - t0 < 0.2                          # the reader is free at once
    assert started.wait(5) and threads == ["commands"]
    agent._on_message({"type": "hello_ok"})                     # handled at once, even while a close waits
    assert agent._last_signature is None and agent._last_report == 0.0   # next poll reports at once
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and not agent.sent:
        time.sleep(0.02)
    assert agent.sent[0]["result"] == "terminated 1"
    agent._commands.put(None)
    worker.join(5)


@pytest.mark.parametrize("msg", [{"type": "close", "pid": "seven", "name": "game.exe"},
                                 {"type": "close", "pid": [1], "name": "game.exe"},
                                 {"type": "close", "pid": 7, "name": {"x": 1}}])
def test_a_malformed_close_still_gets_a_failed_answer(msg, monkeypatch):
    agent = _agent()
    monkeypatch.setattr(agent_main, "close_app", lambda *a: pytest.fail("nothing may be closed"))
    agent._handle(msg)
    assert len(agent.sent) == 1 and agent.sent[0]["type"] == "closed"
    result = agent.sent[0]["result"]
    assert result == agent_main.CLOSE_BAD_REQUEST and result.startswith("failed")   # the service reads "not closed"
    assert isinstance(agent.sent[0]["pid"], int)


def test_a_request_that_raises_is_answered_and_the_thread_goes_on(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(agent, "_close", lambda msg: (_ for _ in ()).throw(RuntimeError("boom")))
    agent._commands.put({"type": "close", "pid": 3, "name": "x.exe"})
    agent._commands.put({"type": "volume", "level": "loud"})   # no audio object: raises, nothing answered
    agent._commands.put(None)
    agent._command_loop()
    assert [m["result"] for m in agent.sent] == [agent_main.CLOSE_BAD_REQUEST]


def test_one_screenshot_at_a_time_and_the_width_is_capped(monkeypatch):
    agent = _agent()
    gate, widths = threading.Event(), []

    def capture(width, fmt, quality, max_bytes=None):
        widths.append(width)
        gate.wait(5)
        return {"error": "test"}

    monkeypatch.setattr(agent_main, "capture_screen", capture)
    agent._handle({"type": "screenshot", "id": "a", "max_width": 100000, "notify": False})
    deadline = time.time() + 5
    while time.time() < deadline and not widths:
        time.sleep(0.01)
    agent._handle({"type": "screenshot", "id": "b", "max_width": 0})
    assert agent.sent[0]["id"] == "b" and "busy" in agent.sent[0]["error"]   # refused, not queued
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and not agent._shot_lock.acquire(blocking=False):
        time.sleep(0.01)
    agent._shot_lock.release()
    assert widths == [agent_main.MAX_CAPTURE_WIDTH] and agent_main.MAX_CAPTURE_WIDTH <= 3840
    agent._handle({"type": "screenshot", "id": "c", "max_width": 0, "notify": False})   # the next one is taken again
    deadline = time.time() + 5
    while time.time() < deadline and len(widths) < 2:
        time.sleep(0.01)
    assert widths[1] == agent_main.MAX_CAPTURE_WIDTH                                     # 0 = full size, capped too


# -- 5. one failing source does not lose the report -----------------------------------------------------

def test_failing_audio_and_presence_still_give_a_report():
    agent = _agent()
    agent.session_id = 2
    agent.client.connected.set()
    agent.activity = types.SimpleNamespace(is_locked=lambda: False, idle_seconds=lambda: 12.0, foreground=lambda: None,
                                           last_hwnd=0, last_created=0.0)

    def broken():
        raise OSError("audio endpoint gone")

    agent.audio = types.SimpleNamespace(active=broken, volume=broken, muted=lambda: True)
    agent.presence = types.SimpleNamespace(sample=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no presence")))
    agent.media = types.SimpleNamespace(read=lambda fg, audio: None)
    agent.urls, agent.browsers = None, set()
    agent.want_audio = agent.volume_control = True
    agent.audio_poll = agent.media_poll = 0.0
    agent._last_audio_poll = agent._last_media_poll = 0.0
    agent._audio_active, agent._volume, agent._muted, agent._media = True, 40, None, None
    agent.heartbeat = 15.0
    agent._sample_and_report()
    report = [m for m in agent.sent if m.get("type") == "report" or "idle_seconds" in m][-1]
    assert report["input_idle_seconds"] == 12.0 and report["idle_seconds"] == 12.0
    assert report["audio_active"] is True and report["volume"] == 40 and report["muted"] is True   # last values kept


# -- 7. a held controller button is not activity for ever -----------------------------------------------

def test_gamepad_counts_only_changes():
    from pclink.agent.win_presence import XINPUT_DEADZONE, gamepad_used

    rest = (10, 0x1000, 0, 0, (0, 0, 0, 0))       # A held down
    assert not gamepad_used(None, 10, 0x1000, 0, 0, (0, 0, 0, 0))                 # the first look only records
    assert not gamepad_used(rest, 10, 0x1000, 0, 0, (0, 0, 0, 0))                 # same packet: nothing happened
    assert not gamepad_used(rest, 11, 0x1000, 0, 0, (500, -300, 0, 0))            # still held, stick noise
    assert gamepad_used(rest, 12, 0, 0, 0, (0, 0, 0, 0))                          # released
    assert gamepad_used(rest, 12, 0x1000, 200, 0, (0, 0, 0, 0))                   # trigger pulled
    assert gamepad_used(rest, 12, 0x1000, 0, 0, (XINPUT_DEADZONE + 1, 0, 0, 0))   # stick moved
    held_trigger = (5, 0, 255, 0, (0, 0, 0, 0))
    assert not gamepad_used(held_trigger, 6, 0, 250, 0, (0, 0, 0, 0))            # a weight on the trigger


# -- 8. waking the display is not somebody at the seat ---------------------------------------------------

def test_injected_wake_up_does_not_reset_idle():
    from pclink.agent.win_activity import idle_after_injection

    # idle for 600 s, the agent injects at tick 1_000_000, Windows records the input at 1_000_005
    assert idle_after_injection(1_000_005, 1_030_000, 1_000_000, 600.0) == pytest.approx(630.0)
    assert idle_after_injection(990_000, 1_010_000, 1_000_000, 600.0) == pytest.approx(610.0)   # not registered (yet)
    assert idle_after_injection(1_020_000, 1_030_000, 1_000_000, 600.0) is None                 # real input since
    # across the 49.7-day wrap of the tick counter
    assert idle_after_injection(3, 2_003, 0xFFFFFFFF - 10, 5.0) == pytest.approx(7.014)


def test_wake_display_remembers_the_injection(monkeypatch):
    from pclink.agent import win_activity as w

    act = w.Win32Activity.__new__(w.Win32Activity)
    act._injected = None
    if not w._IS_WIN:
        pytest.skip("Windows only")
    act._user32 = w._typed_user32()
    act._kernel32 = types.SimpleNamespace(GetTickCount=lambda: 5_000_000)
    monkeypatch.setattr(act, "idle_seconds", lambda: 321.0)
    monkeypatch.setattr(w, "set_display_power", lambda on: True)
    assert act.wake_display() and act._injected == (5_000_000, 321.0)


# -- 9. browser address reading ------------------------------------------------------------------------

def test_uia_rests_and_comes_back_with_backoff(monkeypatch):
    from pclink.agent import browser_url as b

    assert b.cooldown_seconds(1) == b.COOLDOWN_SECONDS and b.cooldown_seconds(2) == 2 * b.COOLDOWN_SECONDS
    assert b.cooldown_seconds(50) == b.MAX_COOLDOWN_SECONDS
    reader = b.BrowserUrlReader("title")
    reader.method = "uia"
    calls = []
    monkeypatch.setattr(reader, "_poll_reader", lambda hwnd, title, now: calls.append(now))
    for _ in range(b.MAX_ERRORS):
        reader._take((1, "t", b.READ_ERROR, None))
    assert reader.method == "uia" and reader._resting_until > time.monotonic() + b.COOLDOWN_SECONDS - 5
    assert reader.url_and_domain(1, "news - example.com")[1] == "example.com" and calls == []   # titles meanwhile
    reader._resting_until = 0.0
    reader.url_and_domain(1, "x")
    assert len(calls) == 1                                       # UI Automation is tried again
    reader._take((1, "x", b.READ_URL, "a.example.com"))
    assert reader._rests == 0


def test_address_bar_search_never_searches_the_whole_window():
    from pclink.agent.browser_url import BrowserUrlReader

    class Element:
        def __init__(self, ctype, cls="", children=(), found=None):
            self.props = {30003: ctype, 30012: cls, 30005: "", 30011: ""}
            self.children, self.found, self.searched = list(children), found, 0

        def GetCachedPropertyValue(self, prop_id):
            return self.props[prop_id]

        def FindAllBuildCache(self, scope, condition, request):
            assert scope == 2
            kids = self.children
            return types.SimpleNamespace(Length=len(kids), GetElement=lambda i: kids[i])

        def FindFirst(self, scope, condition):
            self.searched += 1
            return self.found

    bar = types.SimpleNamespace(GetCurrentPropertyValue=lambda prop: {30008: False, 30045: "example.com/x"}[prop])
    page = Element(50030)
    toolbar = Element(50021, found=bar)
    root = Element(50032, children=[page, Element(50033, children=[toolbar])])
    uia = types.SimpleNamespace(CreateCacheRequest=lambda: types.SimpleNamespace(AddProperty=lambda p: None),
                                CreateTrueCondition=lambda: object(), CreatePropertyCondition=lambda p, v: (p, v),
                                CreateOrCondition=lambda a, b: (a, b), ElementFromHandle=lambda hwnd: root)
    assert BrowserUrlReader._toolbar_search(uia, root) is bar
    assert toolbar.searched == 1 and root.searched == 0 and page.searched == 0     # only inside the tool bar
    reader = BrowserUrlReader("title")
    root.searched = 0
    assert reader._read_omnibox(uia, 1) == ("url", "example.com/x")
    assert root.searched == 0


# -- 10. discovery ---------------------------------------------------------------------------------------

def test_virtual_adapters_are_not_announced():
    from pclink.discovery import announceable, virtual_adapter

    for name in ("vEthernet (WSL)", "vEthernet (Default Switch)", "VirtualBox Host-Only Network", "VMware Network Adapter VMnet8",
                 "Tailscale", "ZeroTier One [8056c2e21c000001]", "Loopback Pseudo-Interface 1", "docker0", "br-1a2b", "lo"):
        assert virtual_adapter(name), name
    for name in ("Ethernet", "Wi-Fi", "Local Area Connection", "vEthernet (External LAN)"):
        assert not virtual_adapter(name), name
    assert not announceable("169.254.3.4") and not announceable("127.0.0.1") and announceable("192.168.1.20")


def _fake_zeroconf(calls, fail_register=False):
    class FakeInfo:
        def __init__(self, type_, name, addresses, port, properties, server):
            self.addresses, self.properties = addresses, properties

    class FakeZeroconf:
        def __init__(self, ip_version=None):
            calls.append("new")

        def register_service(self, info):
            if fail_register:
                raise RuntimeError("name taken")
            calls.append("register")

        def update_service(self, info):
            calls.append(("update", info.addresses))

        def unregister_service(self, info):
            calls.append("unregister")

        def close(self):
            calls.append("close")

    return types.SimpleNamespace(Zeroconf=FakeZeroconf, ServiceInfo=FakeInfo, IPVersion=types.SimpleNamespace(V4Only="v4"))


def test_announcement_follows_address_changes_and_has_no_version(monkeypatch):
    from pclink import discovery
    from pclink.config import ApiConfig

    calls = []
    monkeypatch.setitem(sys.modules, "zeroconf", _fake_zeroconf(calls))
    current = ["192.168.1.20"]
    monkeypatch.setattr(discovery, "lan_addresses", lambda host: list(current))
    adv = discovery.MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", token="t"), "pc", "PC")
    assert adv.start() and "version" not in adv.properties()
    assert adv._thread is not None and adv._thread.is_alive()
    assert adv.refresh() is False                                  # nothing changed
    current[:] = ["192.168.1.77"]
    assert adv.refresh() is True and calls[-1] == ("update", [bytes([192, 168, 1, 77])])
    assert adv.start()                                             # started again: the old one is ended first
    assert calls.count("close") == 1
    adv.stop()
    assert not adv.active and adv._stop.is_set()


def test_failed_registration_closes_zeroconf(monkeypatch):
    from pclink import discovery
    from pclink.config import ApiConfig

    calls = []
    monkeypatch.setitem(sys.modules, "zeroconf", _fake_zeroconf(calls, fail_register=True))
    adv = discovery.MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", token="t"), "pc", "PC", addresses=["10.0.0.2"])
    with pytest.raises(RuntimeError):
        adv.start()
    assert calls == ["new", "close"] and not adv.active


def test_the_integration_does_not_read_the_version_from_the_announcement():
    import pathlib

    flow = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pclink" / "config_flow.py"
    assert 'props.get("version")' not in flow.read_text(encoding="utf-8")


# -- 11 / 13. hardware sensors ---------------------------------------------------------------------------

def test_hardware_backend_is_made_on_the_worker(monkeypatch):
    from pclink.config import HardwareSensorsConfig
    from pclink.hwsensors import HardwareSensors

    hs = HardwareSensors(HardwareSensorsConfig(backend="lhm_http"))
    made_on = []
    started = threading.Event()

    def create(quiet=False):
        made_on.append(threading.current_thread().name)
        started.set()
        time.sleep(0.5)
        return None

    monkeypatch.setattr(hs, "_create_backend", create)
    t0 = time.monotonic()
    hs.start()
    assert time.monotonic() - t0 < 0.3                       # the service's start is not held up
    assert started.wait(5) and made_on == ["hwsensors"]
    hs.stop()


def test_lhm_winring0_versions():
    from pclink.hwsensors import lhm_uses_winring0

    assert lhm_uses_winring0((0, 9, 4)) is True and lhm_uses_winring0((0, 9, 3, 0)) is True
    assert lhm_uses_winring0((0, 9, 5)) is False and lhm_uses_winring0((1, 0, 0)) is False
    assert lhm_uses_winring0(None) is None


def test_duplicate_sensor_names_get_keys_independent_of_order():
    from pclink.hwsensors import unique_keys

    bases = ["nvme_temperature_temperature", "cpu_package_temperature", "nvme_temperature_temperature"]
    ids = ["/nvme/1/temperature/0", "/amdcpu/0/temperature/2", "/nvme/0/temperature/0"]
    keys = unique_keys(bases, ids)
    assert keys[1] == "cpu_package_temperature"                                   # unique names unchanged
    assert keys[2] == "nvme_temperature_temperature"                              # lowest identifier keeps the plain key
    assert keys[0] == "nvme_temperature_temperature_nvme_1_temperature_0"
    flipped = unique_keys(list(reversed(bases)), list(reversed(ids)))
    assert sorted(flipped) == sorted(keys)                                        # the order does not matter
    assert dict(zip(reversed(ids), flipped)) == dict(zip(ids, keys))
    assert unique_keys(["a", "a", "a"], [None, None, None]) == ["a", "a_2", "a_3"]   # no identifier: numbered


def test_lhm_backend_keys_come_from_identifiers():
    from pclink.hwsensors import LhmLibBackend

    class Sensor:
        def __init__(self, ident):
            self.Name, self.Value, self.SensorType, self.Identifier = "Temperature", 40.0, "Temperature", ident

    class Hw:
        def __init__(self, idents):
            self.Name, self.HardwareType, self.SubHardware = "Samsung SSD", "Storage", []
            self.Sensors = [Sensor(i) for i in idents]

        def Update(self):
            pass

    backend = LhmLibBackend.__new__(LhmLibBackend)
    backend.cfg = types.SimpleNamespace(storage_interval_seconds=300.0)
    backend._last_storage = 0.0
    backend._computer = types.SimpleNamespace(Hardware=[Hw(["/nvme/1/temperature/0", "/nvme/0/temperature/0"])])
    first = {r.key for r in backend.read()}
    backend._computer = types.SimpleNamespace(Hardware=[Hw(["/nvme/0/temperature/0", "/nvme/1/temperature/0"])])
    assert {r.key for r in backend.read()} == first and len(first) == 2


# -- 12. disk usage is not read on the main loop ---------------------------------------------------------

def test_disk_usage_runs_on_its_own_thread(monkeypatch):
    from pclink import telemetry
    from pclink.config import TelemetryConfig

    cfg = TelemetryConfig()
    cfg.top_processes = 0
    t = telemetry.Telemetry(cfg)
    threads = []
    monkeypatch.setattr(t, "disk_usage", lambda: threads.append(threading.current_thread().name) or {"disks": ["c"]})
    t._disks._fn = t.disk_usage
    try:
        snap = t.snapshot()
        assert snap.get("disks") == ["c"] and threads and all(name == "bg-disk-usage" for name in threads)
    finally:
        t.stop()


# -- 14. the GPU counter query is rebuilt now and then ---------------------------------------------------

def test_pdh_query_is_rebuilt_every_few_hours():
    from pclink.gpu import PdhGpu

    from .test_fix_agent import FakePdh

    pdh = FakePdh()
    gpu = PdhGpu(pdh)
    pdh.values[PdhGpu.ENGINE_PATH] = {"pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D": 10.0}
    assert gpu.read()["load"] == 10.0 and pdh.closed == 0
    gpu._built_at -= PdhGpu.REBUILD_EVERY_SECONDS + 1
    assert gpu.read()["load"] == 10.0                  # the reading is kept across the rebuild
    assert pdh.closed == 1 and len(pdh.english) == 4   # the old query closed, a new one built
    assert gpu.read()["load"] == 10.0
