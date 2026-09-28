import sys
import types

from pclink.config import ApiConfig
from pclink.discovery import SERVICE_TYPE, MdnsAdvertiser, lan_addresses


def test_lan_addresses_for_explicit_and_wildcard_binds():
    assert lan_addresses("192.168.1.5") == ["192.168.1.5"]
    for addr in lan_addresses("0.0.0.0"):
        assert not addr.startswith(("127.", "169.254."))


def test_loopback_api_is_not_advertised():
    adv = MdnsAdvertiser(ApiConfig(enabled=True, host="127.0.0.1", token="t"), "pc1", "PC 1")
    assert adv.start() is False and not adv.active
    adv.stop()  # no-op


def test_missing_zeroconf_package_is_tolerated(monkeypatch):
    monkeypatch.setitem(sys.modules, "zeroconf", None)  # makes `import zeroconf` raise ImportError
    adv = MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", token="t"), "pc1", "PC 1", addresses=["10.0.0.2"])
    assert adv.start() is False


def test_registers_and_unregisters_service(monkeypatch):
    calls = []

    class FakeInfo:
        def __init__(self, type_, name, addresses, port, properties, server):
            self.type_, self.name, self.addresses, self.port, self.properties, self.server = type_, name, addresses, port, properties, server

    class FakeZeroconf:
        def __init__(self, ip_version=None):
            calls.append(("new", ip_version))

        def register_service(self, info):
            calls.append(("register", info))

        def unregister_service(self, info):
            calls.append(("unregister", info))

        def close(self):
            calls.append(("close",))

    fake = types.SimpleNamespace(Zeroconf=FakeZeroconf, ServiceInfo=FakeInfo, IPVersion=types.SimpleNamespace(V4Only="v4"))
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    adv = MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", port=47392, token="t"), "family_pc", "Family PC",
                         addresses=["10.0.0.2"])
    assert adv.start() is True and adv.active
    info = calls[1][1]
    assert calls[1][0] == "register" and info.type_ == SERVICE_TYPE and info.name == "family-pc." + SERVICE_TYPE
    assert info.port == 47392 and info.addresses == [bytes([10, 0, 0, 2])]
    assert info.properties["id"] == "family_pc" and info.properties["name"] == "Family PC" and info.properties["path"] == "/api"
    adv.stop()
    assert [c[0] for c in calls] == ["new", "register", "unregister", "close"] and not adv.active
