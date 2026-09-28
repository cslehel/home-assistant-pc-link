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
