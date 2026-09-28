"""MQTT connection to Home Assistant with discovery support."""
from __future__ import annotations

import json
import logging
import ssl
import threading
from typing import Any, Callable, Optional

from .config import MqttConfig

log = logging.getLogger(__name__)

try:  # paho-mqtt >= 2
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - dependency missing
    mqtt = None  # type: ignore[assignment]


CommandHandler = Callable[[str, Any], None]


class MqttBridge:
    """Thin wrapper around paho: topics, LWT, discovery, command dispatch.

    Topic layout (``base`` = ``<base_topic>/<device_id>``):

    * ``base/availability``            online / offline (retained, LWT)
    * ``base/state``                   PC telemetry JSON
    * ``base/user/<slug>/state``       per-user JSON
    * ``base/set/<object_id>``         commands from HA entities (switch/number/button/text)
    * ``base/cmd``                     free-form JSON commands
    """

    def __init__(self, cfg: MqttConfig, device_id: str, on_command: CommandHandler,
                 on_connected: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.device_id = device_id
        self.base = f"{cfg.base_topic}/{device_id}"
        self.availability_topic = f"{self.base}/availability"
        self.state_topic = f"{self.base}/state"
        self.cmd_topic = f"{self.base}/cmd"
        self.set_prefix = f"{self.base}/set/"
        self.discovery_prefix = cfg.discovery_prefix
        self._on_command = on_command
        self._on_connected = on_connected
        self._client = None
        self._connected = threading.Event()
        self._lock = threading.Lock()

    # -- topics ---------------------------------------------------------------
    def user_state_topic(self, slug: str) -> str:
        return f"{self.base}/user/{slug}/state"

    def set_topic(self, object_id: str) -> str:
        return f"{self.set_prefix}{object_id}"

    def discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/{self.device_id}/{object_id}/config"

    # -- lifecycle ------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if mqtt is None:
            raise RuntimeError("paho-mqtt is not installed")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.client_id, clean_session=True)
        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
        if self.cfg.tls:
            client.tls_set(ca_certs=self.cfg.tls_ca or None, cert_reqs=ssl.CERT_NONE if self.cfg.tls_insecure else ssl.CERT_REQUIRED)
            if self.cfg.tls_insecure:
                client.tls_insecure_set(True)
        client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self._client = client
        try:
            client.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive)
        except (OSError, ValueError) as exc:
            log.error("MQTT connect setup failed: %s", exc)
        client.loop_start()

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            if self.connected:
                client.publish(self.availability_topic, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._client = None
        self._connected.clear()

    # -- callbacks ------------------------------------------------------------
    def _handle_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: D401
        if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0):
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self.cfg.host, self.cfg.port)
        self._connected.set()
        client.subscribe([(self.set_prefix + "#", 1), (self.cmd_topic, 1), (f"{self.discovery_prefix}/status", 0)])
        client.publish(self.availability_topic, "online", qos=1, retain=True)
        if self._on_connected:
            try:
                self._on_connected()
            except Exception:  # noqa: BLE001
                log.exception("on_connected handler failed")

    def _handle_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        self._connected.clear()
        log.warning("MQTT disconnected (%s); paho will retry", reason_code)

    def _handle_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            payload = ""
        topic = msg.topic
        try:
            if topic == f"{self.discovery_prefix}/status":
                if payload.strip().lower() == "online" and self._on_connected:
                    log.info("Home Assistant came online; republishing discovery")
                    self._on_connected()
                return
            if topic == self.cmd_topic:
                try:
                    data = json.loads(payload) if payload.strip() else {}
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON command: %r", payload[:200])
                    return
                self._on_command("cmd", data)
                return
            if topic.startswith(self.set_prefix):
                object_id = topic[len(self.set_prefix):]
                self._on_command(object_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("command handling failed for %s", topic)

    # -- publishing -----------------------------------------------------------
    def publish(self, topic: str, payload: Any, retain: bool = False, qos: Optional[int] = None) -> None:
        client = self._client
        if client is None:
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, separators=(",", ":"), default=_json_default)
        elif payload is None:
            payload = ""
        elif isinstance(payload, bool):
            payload = "ON" if payload else "OFF"
        elif not isinstance(payload, (str, bytes)):
            payload = str(payload)
        try:
            client.publish(topic, payload, qos=self.cfg.qos if qos is None else qos, retain=retain)
        except Exception as exc:  # noqa: BLE001
            log.debug("publish to %s failed: %s", topic, exc)

    def publish_discovery(self, component: str, object_id: str, payload: Optional[dict]) -> None:
        topic = self.discovery_topic(component, object_id)
        if payload is None:
            self.publish(topic, "", retain=True, qos=1)
        else:
            self.publish(topic, payload, retain=True, qos=1)

    def publish_availability(self, online: bool = True) -> None:
        self.publish(self.availability_topic, "online" if online else "offline", retain=True, qos=1)


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)
