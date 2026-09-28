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


def test_new_user_entities_follow_policy():
    import yaml
    cfg = Config.from_dict(yaml.safe_load("""
        device: {name: TestPC}
        screenshots: {enabled: true}
        time_limits:
          parent_pin: 1
          users:
            - name: kevin
              daily_limit_minutes: 60
              weekly_limit_minutes: 300
              rollover_max_minutes: 20
              bedtime: "21:00"
              rules:
                - {name: YouTube, sites: [youtube.com], daily_limit_minutes: 30}
                - {name: TikTok, sites: [tiktok.com], blocked: true}
              rewards: [{name: Chores, minutes: 10}]
            - name: dad
              unlimited: true
        apps: [{name: Word, launch: winword.exe}]
        windows_update: {enabled: true, allow_install: true}
        telemetry: {top_processes: 3, usb_devices: true, printers: true}
    """))
    f = EntityFactory(cfg, base_topic="pclink/testpc", availability_topic="a", state_topic="s")
    ents = f.user_entities("kevin", cfg.user_policy("kevin"), managed=True)
    objs = {e.object_id: e for e in ents}
    for oid in ("user_kevin_presence", "user_kevin_now_playing", "user_kevin_weekly_limit", "user_kevin_weekly_remaining",
                "user_kevin_rollover", "user_kevin_bedtime", "user_kevin_bedtime_extension", "user_kevin_extend_bedtime_30",
                "user_kevin_focus_mode", "user_kevin_focus_minutes", "user_kevin_breaks_enabled", "user_kevin_continuous_use",
                "user_kevin_rule_youtube", "user_kevin_rule_youtube_enabled", "user_kevin_rule_youtube_remaining",
                "user_kevin_rule_youtube_limit_today", "user_kevin_rule_tiktok", "user_kevin_rule_tiktok_enabled",
                "user_kevin_reward_chores", "user_kevin_add_minutes", "user_kevin_screenshot", "user_kevin_volume", "user_kevin_muted"):
        assert oid in objs, oid
    assert "user_kevin_rule_tiktok_limit_today" not in objs   # a blocklist entry has no limit number
    cams = [e for e in ents if e.component == "camera"]
    assert cams and cams[0].payload["topic"] == "pclink/testpc/user/kevin/screenshot"
    ids = [e.unique_id for e in ents]
    assert len(ids) == len(set(ids))
    for e in ents:
        json.dumps(e.payload)
    dad = {e.object_id for e in f.user_entities("dad", cfg.user_policy("dad"), managed=False)}
    assert "user_dad_screenshot" not in dad and "user_dad_focus_mode" not in dad and "user_dad_now_playing" in dad
    triggers = f.user_trigger_entities("kevin", cfg.user_policy("kevin"))
    assert all(t.component == "device_automation" for t in triggers)
    assert {t.payload["payload"] for t in triggers} >= {"time_up", "bedtime", "focus_started", "break_due"}
    pc = {e.object_id: e for e in f.pc_entities({"disks": [], "gpu_name": None})}
    for oid in ("parent_pin", "parent_unlocked", "parent_present", "reload_config", "launch_word", "close_word",
                "pending_updates", "reboot_pending", "check_updates", "install_updates", "top_process", "usb_devices",
                "printer_status", "anyone_present"):
        assert oid in pc, oid
    assert pc["parent_pin"].payload["mode"] == "password"
    fleet = f.fleet_entities({"kevin": {"name": "Kevin"}}, "pclink/fleet/state", lambda slug: f"pclink/fleet/user/{slug}/state")
    assert all(e.payload["device"]["identifiers"] == ["pclink_fleet"] for e in fleet)
    assert {e.object_id for e in fleet} >= {"fleet_pcs_online", "fleet_user_kevin_total_today", "fleet_any_pc_in_use"}


# -- catalog for the native integration (GET /api/entities) ----------------------------------------

CATALOG_PLATFORMS = {"sensor", "binary_sensor", "switch", "number", "button", "text", "camera"}


def test_pc_catalog_entries_are_transport_neutral():
    cfg, f = make_factory()
    ents = f.pc_entities({"disks": ["c"], "gpu_name": "RTX", "battery_percent": 90})
    entries = [e.catalog_entry() for e in ents]
    entries = [e for e in entries if e]
    keys = {e["key"] for e in entries}
    assert {"cpu_percent", "shutdown", "time_limits_enabled", "broadcast_message", "parent_present"} <= keys
    assert "online" not in keys  # availability-based power sensor is MQTT-only
    for e in entries:
        assert e["source"] == "pc" and e["platform"] in CATALOG_PLATFORMS
        assert e["unique_id"].startswith("pclink_testpc_")
        assert "topic" not in json.dumps(e) and "template" not in json.dumps(e)
    cpu = next(e for e in entries if e["key"] == "cpu_percent")
    assert cpu == {"unique_id": "pclink_testpc_cpu_percent", "key": "cpu_percent", "platform": "sensor",
                   "name": "CPU usage", "value": "cpu_percent", "unit": "%", "state_class": "measurement",
                   "icon": "mdi:cpu-64-bit", "enabled": True, "precision": 0, "source": "pc"}
    off = next(e for e in entries if e["key"] == "cpu_frequency")
    assert off["enabled"] is False and off["device_class"] == "frequency"
    switch = next(e for e in entries if e["key"] == "time_limits_enabled")
    assert switch["platform"] == "switch" and switch["command"] == "time_limits_enabled" and switch["category"] == "config"
    button = next(e for e in entries if e["key"] == "reboot")
    assert button["platform"] == "button" and button["device_class"] == "restart" and "value" not in button


def test_user_catalog_entries_carry_user_and_commands():
    cfg, f = make_factory()
    entries = [e.catalog_entry() for e in f.user_entities("kevin", cfg.user_policy("kevin"), managed=True)]
    entries = [e for e in entries if e]
    for e in entries:
        assert e["source"] == "user" and e["user"] == "kevin" and e["user_name"] == "kevin" and e["managed"] is True
    blocked = next(e for e in entries if e["key"] == "user_kevin_blocked")
    assert blocked["platform"] == "switch" and blocked["value"] == "blocked" and blocked["command"] == "user_kevin_blocked"
    limit = next(e for e in entries if e["key"] == "user_kevin_limit_today")
    assert limit["platform"] == "number" and limit["value"] == "limit_today"
    assert (limit["min"], limit["max"], limit["step"], limit["unit"], limit["mode"]) == (0, 1440, 5, "min", "box")
    remaining = next(e for e in entries if e["key"] == "user_kevin_time_remaining")
    assert remaining["attributes"] == "limit_details" and remaining["device_class"] == "duration"
    status = next(e for e in entries if e["key"] == "user_kevin_status")
    assert status["device_class"] == "enum" and "active" in status["options"]
    message = next(e for e in entries if e["key"] == "user_kevin_message")
    assert message["platform"] == "text" and "value" not in message and "password" not in message


def test_password_text_and_camera_specs():
    import yaml
    cfg = Config.from_dict(yaml.safe_load("""
        device: {name: TestPC}
        screenshots: {enabled: true}
        time_limits:
          parent_pin: "1234"
          users: [{name: kevin, daily_limit_minutes: 60}]
    """))
    f = EntityFactory(cfg, base_topic="pclink/testpc", availability_topic="a", state_topic="s")
    pc = {e["key"]: e for e in (x.catalog_entry() for x in f.pc_entities({})) if e}
    assert pc["parent_pin"]["platform"] == "text" and pc["parent_pin"]["password"] is True
    assert pc["parent_pin"]["value"] == "parent_pin_state"
    user = {e["key"]: e for e in (x.catalog_entry() for x in f.user_entities("kevin", cfg.user_policy("kevin"), True)) if e}
    cam = user["user_kevin_screenshot_image"]
    assert cam["platform"] == "camera" and cam["value"] == "screenshot" and cam["user"] == "kevin"


def test_mqtt_only_items_have_no_catalog_entry():
    cfg, f = make_factory()
    assert all(e.catalog_entry() is None for e in f.user_trigger_entities("kevin", cfg.user_policy("kevin")))
    assert all(e.catalog_entry() is None for e in f.pc_trigger_entities())
    fleet = f.fleet_entities({"kevin": {"name": "Kevin"}}, "pclink/fleet/state", lambda slug: f"pclink/fleet/user/{slug}")
    assert fleet and all(e.catalog_entry() is None for e in fleet)
    online = next(e for e in f.pc_entities({}) if e.object_id == "online")
    assert online.catalog_entry() is None
