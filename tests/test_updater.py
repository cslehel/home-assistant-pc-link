"""Self-update: version comparison, release selection, download checks, the Home Assistant commands."""
import hashlib
import os
import zipfile
from datetime import datetime

import pytest
import yaml

from pclink import updater as up
from pclink.config import Config, ConfigError, UpdatesConfig
from pclink.core import Orchestrator
from pclink.entities import EntityFactory
from pclink.store import Store

from .test_orchestrator import FakeApi, FakeBlocker, FakeSessions


def release(tag="v0.4.0", digest="sha256:" + "0" * 64, **extra):
    return {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}", "body": "Notes\n\n- a fix",
            "assets": [{"name": f"PCLink-{tag.lstrip('v')}-windows-x64.zip", "digest": digest,
                        "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/PCLink.zip"}], **extra}


def make(installed="0.3.0", fetch=None, can_install=True, tmp_path=None, **cfg):
    events = []
    upd = up.SelfUpdate(UpdatesConfig(**cfg), str(tmp_path or "unused"), on_change=events.append, installed=installed,
                        can_install=can_install, fetch=fetch or (lambda url: release()))
    return upd, events


def test_version_comparison():
    assert up.is_newer("v0.4.0", "0.3.9") and up.is_newer("1.0", "0.9.9") and up.is_newer("0.3.0.1", "0.3.0")
    assert not up.is_newer("0.3.0", "0.3.0") and not up.is_newer("0.3", "0.3.0") and not up.is_newer("0.2.9", "0.3.0")
    assert not up.is_newer("latest", "0.3.0") and not up.is_newer("0.4.0", "")


def test_check_reports_a_newer_release_once():
    upd, events = make()
    upd._check()
    upd._check()
    snap = upd.snapshot()
    assert snap["pclink_installed_version"] == "0.3.0" and snap["pclink_latest_version"] == "0.4.0"
    assert snap["pclink_update_available"] is True and snap["pclink_update_info"]["can_install"] is True
    assert snap["pclink_update_release_url"].startswith("https://github.com/")
    assert events == ["pclink_update_available"]


def test_same_or_older_release_is_not_an_update():
    upd, events = make(installed="0.4.0")
    upd._check()
    assert upd.snapshot()["pclink_update_available"] is False and events == []
    assert upd.install() == (False, "no newer version is known; check for updates first")


def test_failed_check_keeps_running():
    def boom(url):
        raise OSError("offline")

    upd, _ = make(fetch=boom)
    upd._check()
    snap = upd.snapshot()
    assert snap["pclink_update_info"]["status"] == "error" and snap["pclink_latest_version"] == "0.3.0"


def test_prereleases_and_drafts_are_skipped_unless_asked_for():
    releases = [release("v0.5.0", prerelease=True), release("v0.4.1"), release("v0.6.0", draft=True)]
    assert up.pick_release(releases, False)["tag_name"] == "v0.4.1"
    assert up.pick_release(releases, True)["tag_name"] == "v0.5.0"
    assert up.pick_release([{"tag_name": "nightly"}], True) is None


def test_install_is_refused_when_not_possible():
    upd, _ = make(can_install=False)
    upd._check()
    assert upd.install()[0] is False and upd.snapshot()["pclink_update_info"]["can_install"] is False
    upd, _ = make(allow_install=False)
    upd._check()
    assert upd.install()[0] is False
    upd, _ = make(fetch=lambda url: dict(release(), assets=[]))
    upd._check()
    assert upd.install() == (False, "release 0.4.0 has no Windows download")


def test_download_must_come_from_github_with_a_matching_checksum(tmp_path, monkeypatch):
    payload = b"zip bytes"

    class Resp:
        def __init__(self):
            self.data = payload

        def read(self, n=-1):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(up, "_open", lambda url, accept, token=None: Resp())
    upd, _ = make()
    good = "sha256:" + hashlib.sha256(payload).hexdigest()
    url = "https://github.com/o/r/releases/download/v0.4.0/PCLink.zip"
    upd._download({"browser_download_url": url, "digest": good}, str(tmp_path / "ok.zip"))
    assert (tmp_path / "ok.zip").read_bytes() == payload
    for asset in ({"browser_download_url": url, "digest": "sha256:" + "1" * 64},
                  {"browser_download_url": url},
                  {"browser_download_url": "https://example.com/PCLink.zip", "digest": good},
                  {"browser_download_url": "http://github.com/o/r/PCLink.zip", "digest": good},
                  {"browser_download_url": "https://github.com.evil.example/PCLink.zip", "digest": good}):
        with pytest.raises(ValueError):
            upd._download(asset, str(tmp_path / "bad.zip"))


def test_archive_cannot_write_outside_the_staging_folder(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("PCLink/pclink.exe", "x")
    up.safe_extract(str(good), str(tmp_path / "stage"))
    assert (tmp_path / "stage" / "PCLink" / "pclink.exe").is_file()
    for name in ("../escaped.txt", "PCLink/../../escaped.txt", "C:/escaped.txt", "/escaped.txt"):
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(name, "x")
        with pytest.raises(ValueError):
            up.safe_extract(str(bad), str(tmp_path / "stage2"))
    assert not (tmp_path / "escaped.txt").exists()


def test_failed_install_is_reported_and_cleaned_up(tmp_path, monkeypatch):
    upd, _ = make(tmp_path=tmp_path / "update")
    upd._check()

    def no_network(asset, target):
        raise ValueError("the download does not match the checksum GitHub published")

    monkeypatch.setattr(upd, "_download", no_network)
    upd._install()
    info = upd.snapshot()["pclink_update_info"]
    assert info["status"] == "error" and "checksum" in info["error"]
    assert not os.path.exists(tmp_path / "update")


def test_config_and_entities():
    with pytest.raises(ConfigError):
        Config.from_dict({"updates": {"repository": "https://evil.example/x"}})
    cfg = Config.from_dict(yaml.safe_load("device: {name: TestPC}\nupdates: {auto_install: true}"))
    assert cfg.updates.enabled and cfg.updates.auto_install and cfg.updates.repository == "cslehel/home-assistant-pc-link"
    factory = EntityFactory(cfg)
    by_id = {e.object_id: e for e in factory.pc_entities({"pclink_update_info": {"can_install": True}})}
    entity = by_id["pclink_update"]
    assert entity.component == "update" and entity.spec["platform"] == "update"
    assert entity.spec["command"] == "pclink_update_install" and "pclink_update_check" in by_id
    # the state of the update (a failed or refused install, the last check) is the entity's attributes
    assert entity.spec["attributes"] == "pclink_update_info"
    cannot = {e.object_id: e for e in factory.pc_entities({})}["pclink_update"]
    assert cannot.spec["command"] is None and "command" not in cannot.catalog_entry()
    off = Config.from_dict(yaml.safe_load("device: {name: TestPC}\nupdates: {enabled: false}"))
    assert "pclink_update" not in {e.object_id for e in EntityFactory(off).pc_entities({})}


def test_orchestrator_commands():
    class FakeUpdater:
        def __init__(self):
            self.calls = []

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return {"pclink_installed_version": "0.3.0", "pclink_latest_version": "0.4.0"}

        def check_now(self):
            self.calls.append("check")

        def install(self):
            self.calls.append("install")
            return False, "no newer version is known; check for updates first"

    fake = FakeUpdater()
    cfg = Config.from_dict(yaml.safe_load("device: {name: TestPC}"))
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi(),
                        clock=lambda: datetime(2026, 9, 28, 12), updater=fake)
    orch.tick()
    assert orch.pc_payload([])["pclink_latest_version"] == "0.4.0"
    assert orch.api_command({"action": "set", "entity": "pclink_update_check", "value": "PRESS"})["ok"] is True
    assert orch.api_command({"action": "set", "entity": "pclink_update_install", "value": "PRESS"})["ok"] is False
    assert orch.api_command({"action": "pclink_update_install"})["error"].startswith("no newer version")
    assert fake.calls == ["check", "install", "install"]
    orch.updater = None
    assert orch.api_command({"action": "pclink_update_check"})["ok"] is False
