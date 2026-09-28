import json

from pclink.config import Config
from pclink.entities import EntityFactory


def make_factory():
    import yaml
    cfg = Config.from_dict(yaml.safe_load("""
        device: {name: TestPC}
        time_limits:
          users:
            - name: kevin
              daily_limit_minutes: 60
            - name: dad
              unlimited: true
    """))
    return cfg, EntityFactory(cfg, base_topic="pclink/testpc", availability_topic="pclink/testpc/availability",
                              state_topic="pclink/testpc/state")


def test_pc_entities_have_unique_ids_and_valid_payloads():
    cfg, f = make_factory()
    ents = f.pc_entities({"disks": ["c", "d"], "gpu_name": "RTX", "battery_percent": 90})
    ids = [e.unique_id for e in ents]
    assert len(ids) == len(set(ids))  # unique
    for e in ents:
        assert e.payload["device"]["identifiers"] == ["pclink_testpc"]
        json.dumps(e.payload)  # serialisable
    # shutdown/reboot buttons present
    objs = {e.object_id for e in ents}
    assert {"shutdown", "reboot", "cpu_percent", "memory_percent", "disk_c_percent", "gpu_load", "battery"} <= objs


def test_managed_user_entities_include_limit_controls():
    cfg, f = make_factory()
    ents = f.user_entities("kevin", cfg.user_policy("kevin"), managed=True)
    objs = {e.object_id for e in ents}
    assert "user_kevin_time_remaining" in objs
    assert "user_kevin_limit_today" in objs
    assert "user_kevin_add_15" in objs
    assert "user_kevin_blocked" in objs
    # number entities target the user state topic and have a command topic
    num = next(e for e in ents if e.object_id == "user_kevin_limit_today")
    assert num.component == "number"
    assert num.payload["command_topic"].endswith("/set/user_kevin_limit_today")


def test_unmanaged_user_has_no_limit_controls():
    cfg, f = make_factory()
    ents = f.user_entities("dad", cfg.user_policy("dad"), managed=False)
    objs = {e.object_id for e in ents}
    assert "user_dad_time_used_today" in objs      # stats still present
    assert "user_dad_time_remaining" not in objs   # but no limit
    assert "user_dad_limit_today" not in objs
