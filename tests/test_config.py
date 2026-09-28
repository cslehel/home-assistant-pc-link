import textwrap

import pytest

from pclink.config import Config, ConfigError


def load(yaml_text):
    import yaml
    return Config.from_dict(yaml.safe_load(textwrap.dedent(yaml_text)))


def test_minimal_config_defaults():
    cfg = load("mqtt: {host: broker}")
    assert cfg.mqtt.host == "broker"
    assert cfg.device.id  # derived from hostname
    assert cfg.mqtt.client_id.startswith("pclink-")


def test_user_policy_by_day():
    cfg = load("""
        time_limits:
          users:
            - name: Kevin
              daily_limit_minutes:
                weekdays: 90
                weekend: 180
                fri: 150
              allowed_hours:
                all: 07:00-20:00
              action: logoff
    """)
    p = cfg.user_policy("kevin")
    assert p is not None
    assert p.daily_limits[0] == 90   # Monday
    assert p.daily_limits[4] == 150  # Friday overrides weekdays
    assert p.daily_limits[5] == 180  # Saturday
    assert p.allowed_windows[0] == [(420, 1200)]
    assert p.slug == "kevin"


def test_unlimited_user_has_no_restrictions():
    cfg = load("""
        time_limits:
          users:
            - name: dad
              unlimited: true
    """)
    p = cfg.user_policy("dad")
    assert p.unlimited and not p.has_restrictions


def test_unknown_action_rejected():
    with pytest.raises(ConfigError):
        load("""
            time_limits:
              users:
                - name: x
                  action: explode
        """)


def test_duplicate_user_rejected():
    with pytest.raises(ConfigError):
        load("""
            time_limits:
              users:
                - name: kevin
                - name: Kevin
        """)


def test_bad_block_method_rejected():
    with pytest.raises(ConfigError):
        load("time_limits: {block_method: nuke}")


def test_commands_and_site_categories():
    cfg = load("""
        activity:
          site_categories:
            "*.youtube.com": video
        commands:
          - name: Mute
            command: nircmd mutesysvolume 1
            run_as: user
    """)
    assert cfg.activity.site_categories["youtube.com"] == "video"
    assert cfg.commands[0].slug == "mute"
    assert cfg.commands[0].run_as == "user"


def test_new_sections_and_user_fields_parse():
    cfg = load("""
        time_limits:
          parent_pin: 1234
          week_start: sun
          shared_budget: true
          breaks: 30
          rules:
            - {name: TikTok, sites: [tiktok.com], blocked: true}
          rewards:
            - {name: Homework, minutes: 20, daily_max: 2}
          users:
            - name: kevin
              daily_limit_minutes: 60
              weekly_limit_minutes: 5h
              rollover_max_minutes: 30
              bedtime: {weekdays: "21:00", weekend: "22:30"}
              wake_time: 07:00
              bedtime_parent_extension_minutes: 45
              count_away: true
              count_passive: false
              focus: [winword.exe, khanacademy.org]
              rules:
                - name: YouTube
                  sites: youtube.com, youtu.be
                  daily_limit_minutes: 30
                  allowed_hours: 16:00-20:00
                  action: warn
        apps:
          Minecraft: 'C:\\Games\\Minecraft.exe'
        windows_update: {enabled: true, allow_install: true}
        screenshots: {enabled: true, users: [Kevin], format: png}
        api: {enabled: true, host: 0.0.0.0, token: abc}
        fleet: {enabled: false}
        telemetry: {top_processes: 5, usb_devices: true, printers: true}
    """)
    tl = cfg.time_limits
    assert tl.pin_enabled and tl.parent_pin == "1234" and tl.week_start_index == 6 and tl.shared_budget
    p = cfg.user_policy("kevin")
    assert p.weekly_limit_minutes == 300 and p.rollover_max_minutes == 30
    assert p.bedtime[0] == 21 * 60 and p.bedtime[5] == 22 * 60 + 30 and p.wake_time[0] == 7 * 60
    assert p.bedtime_parent_extension_minutes == 45 and p.count_away is True and p.count_passive is False
    assert p.focus.allow_apps == ["winword.exe"] and p.focus.allow_sites == ["khanacademy.org"]
    assert [r.name for r in p.rules] == ["TikTok", "YouTube"]
    yt = p.rules[1]
    assert yt.sites == ["youtube.com", "youtu.be"] and yt.action == "warn" and yt.allowed_windows[0] == [(960, 1200)]
    assert p.breaks.enabled and p.breaks.every_minutes == 30
    assert p.rewards[0].daily_max == 2
    assert cfg.apps[0].slug == "minecraft" and cfg.apps[0].process_name == "minecraft.exe"
    assert cfg.windows_update.allow_install and cfg.screenshots.users == ["kevin"] and cfg.screenshots.format == "png"
    assert cfg.api.token == "abc" and not cfg.fleet.enabled and cfg.telemetry.top_processes == 5
    # unknown user with manage_unknown_users inherits shared rules/rewards/breaks
    cfg.time_limits.manage_unknown_users = True
    unknown = cfg.build_default_policy("guest")
    assert [r.name for r in unknown.rules] == ["TikTok"] and unknown.breaks.enabled and unknown.rewards


def test_invalid_new_sections_rejected():
    with pytest.raises(ConfigError):
        load("api: {enabled: true, host: 0.0.0.0}")           # non-loopback needs a token
    with pytest.raises(ConfigError):
        load("screenshots: {format: gif}")
    with pytest.raises(ConfigError):
        load("time_limits: {rules: [{name: x, sites: [a.com]}]}")   # rule without limit/hours/blocked
    with pytest.raises(ConfigError):
        load("time_limits: {rules: [{name: x, blocked: true}]}")    # rule without a match
    with pytest.raises(ConfigError):
        load("time_limits: {rules: [{name: x, apps: [a.exe], blocked: true, action: nuke}]}")
    with pytest.raises(ConfigError):
        load("time_limits: {week_start: someday}")
    with pytest.raises(ConfigError):
        load("time_limits: {users: [{name: k, bedtime: '25:00'}]}")
    with pytest.raises(ConfigError):
        load("apps: [{name: x}]")
