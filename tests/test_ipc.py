import threading
import time

from pclink.ipc import AgentClient, AgentServer
from pclink.models import AgentReport, ForegroundInfo


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_agent_roundtrip_with_token():
    port = free_port()
    received = []
    connects = []
    server = AgentServer("127.0.0.1", port, on_report=lambda sid, r: received.append(r),
                         validate=lambda sid, tok: tok == "good",
                         on_connect=lambda sid, u, p: connects.append(sid))
    server.start()
    try:
        got = threading.Event()

        def on_msg(msg):
            if msg.get("type") == "notify":
                got.set()

        client = AgentClient("127.0.0.1", port,
                             hello={"session_id": 3, "user": "kevin", "token": "good", "pid": 1}, on_message=on_msg)
        client.start()
        assert client.connected.wait(5)
        # send a report from the client
        report = AgentReport(session_id=3, ts=time.time(), foreground=ForegroundInfo(name="chrome.exe"), domain="youtube.com")
        assert client.send(report.to_dict())
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.05)
        assert received and received[0].domain == "youtube.com"
        assert received[0].session_id == 3
        # server -> client message
        assert server.send(3, {"type": "notify", "title": "hi", "message": "there"})
        assert got.wait(5)
        assert connects == [3]
    finally:
        client.stop()
        server.stop()


def test_bad_token_rejected():
    port = free_port()
    server = AgentServer("127.0.0.1", port, on_report=lambda *a: None, validate=lambda sid, tok: tok == "good")
    server.start()
    try:
        client = AgentClient("127.0.0.1", port,
                             hello={"session_id": 9, "user": "x", "token": "bad", "pid": 1},
                             on_message=lambda m: None, reconnect_seconds=0.2)
        client.start()
        assert client.rejected.wait(5)
        assert not client.connected.is_set()
    finally:
        client.stop()
        server.stop()
