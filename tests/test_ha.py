"""MqttBridge behaviour around connection loss (paho client replaced by a fake)."""
from __future__ import annotations

import pclink.ha as ha
from pclink.config import MqttConfig


class _Published:
    def wait_for_publish(self, timeout=None):
        return None


class FakePahoClient:
    """Records configuration calls; never opens a socket."""

    instances: list["FakePahoClient"] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.published: list[tuple] = []
        self.subscriptions: list = []
        self.on_connect = self.on_disconnect = self.on_message = None
        FakePahoClient.instances.append(self)

    def _rec(self, name):
        def method(*a, **k):
            self.calls.append((name, a, k))
        return method

    def __getattr__(self, name):  # any other configuration call is just recorded
        if name.startswith("__"):
            raise AttributeError(name)
        return self._rec(name)

    def subscribe(self, subs):
        self.subscriptions.append(subs)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return _Published()


class _Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


def make_bridge(monkeypatch, connected_cb=None):
    monkeypatch.setattr(ha.mqtt, "Client", FakePahoClient)
    FakePahoClient.instances.clear()
    cfg = MqttConfig(host="broker.local", client_id="pclink-test")
    bridge = ha.MqttBridge(cfg, "testpc", on_command=lambda *_: None, on_connected=connected_cb)
    bridge.start()
    client = FakePahoClient.instances[-1]
    return bridge, client


def _call(client, name):
    return [c for c in client.calls if c[0] == name]


def test_start_configures_lwt_reconnect_and_bounded_offline_queue(monkeypatch):
    bridge, client = make_bridge(monkeypatch)
    will = _call(client, "will_set")
    assert will and will[0][1][:2] == (bridge.availability_topic, "offline") and will[0][2].get("retain") is True
    assert _call(client, "reconnect_delay_set"), "paho automatic reconnection must be configured"
    queued = _call(client, "max_queued_messages_set")
    assert queued and 0 < queued[0][1][0] <= 1000, "offline queue must be bounded"
    assert _call(client, "connect_async") and _call(client, "loop_start")


def test_events_are_dropped_while_disconnected_and_sent_when_connected(monkeypatch):
    connected_calls = []
    bridge, client = make_bridge(monkeypatch, connected_cb=lambda: connected_calls.append(1))
    assert not bridge.connected
    assert bridge.publish_event("user/kevin", "time_up") is False
    assert not any("event" in t for t, *_ in client.published)

    bridge._handle_connect(client, None, {}, 0)  # reason code 0 = success
    assert bridge.connected
    assert connected_calls == [1]
    assert (bridge.availability_topic, "online", 1, True) in client.published
    assert bridge.publish_event("user/kevin", "time_up") is True
    assert any(t.endswith("/event/user/kevin") and p == "time_up" for t, p, *_ in client.published)

    bridge._handle_disconnect(client, None, None, 7)
    assert not bridge.connected
    before = len(client.published)
    assert bridge.publish_event("pc", "started") is False
    assert len(client.published) == before  # nothing queued for a stale replay
    bridge.publish(bridge.state_topic, {"cpu_percent": 1}, retain=True)  # state still handed to paho
    assert len(client.published) == before + 1


def test_home_assistant_birth_message_triggers_resync(monkeypatch):
    connected_calls = []
    bridge, client = make_bridge(monkeypatch, connected_cb=lambda: connected_calls.append(1))
    bridge._handle_connect(client, None, {}, 0)
    bridge._handle_message(client, None, _Msg(f"{bridge.discovery_prefix}/status", b"online"))
    assert connected_calls == [1, 1]
    bridge._handle_message(client, None, _Msg(f"{bridge.discovery_prefix}/status", b"offline"))
    assert connected_calls == [1, 1]


def test_refused_connection_does_not_mark_connected(monkeypatch):
    bridge, client = make_bridge(monkeypatch)
    bridge._handle_connect(client, None, {}, 5)  # 5 = not authorised
    assert not bridge.connected
    assert bridge.publish_event("pc", "started") is False
