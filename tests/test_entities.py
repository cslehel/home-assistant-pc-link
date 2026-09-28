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
