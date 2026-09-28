import os

import yaml

from pclink import example_config
from pclink.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_example_config_parses_and_embedded_copy_is_in_sync():
    with open(os.path.join(ROOT, "config.example.yaml"), "r", encoding="utf-8") as fh:
        text = fh.read()
    cfg = Config.from_dict(yaml.safe_load(text))
    kevin = cfg.user_policy("kevin")
    assert kevin.weekly_limit_minutes == 600 and kevin.rollover_max_minutes == 30 and kevin.has_bedtime
    assert [r.name for r in kevin.rules] == ["TikTok", "Video", "Roblox"]
    assert kevin.focus.allow_apps and not kevin.focus.counts_toward_limit
    assert kevin.breaks.enabled and not cfg.user_policy("sarah").breaks.enabled
    assert [r.name for r in kevin.rewards] == ["Homework done", "Chores"]
    assert cfg.fleet.enabled and not cfg.api.enabled and not cfg.screenshots.enabled
    assert example_config.EMBEDDED == text, "run scripts/sync_example_config.py"
    assert example_config.EXAMPLE_CONFIG == text
