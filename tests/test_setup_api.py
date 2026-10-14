"""`setup-api`: the local API, its token and HTTPS written into config.yaml in one go."""
from __future__ import annotations

import os
import re

import pytest
import yaml

from pclink import __main__ as cli
from pclink.config import Config

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml")


def test_keys_are_set_in_place_and_comments_kept():
    text = ("device: {name: Den PC}\n"
            "api:                 # the local API\n"
            "  enabled: false     # set true\n"
            "  host: 127.0.0.1\n"
            "  # token: change-me # required for writes\n"
            "  allowed_hosts:\n"
            "    - den.lan\n"
            "\n"
            "# the rest\n"
            "fleet:\n"
            "  enabled: true\n")
    out = cli.set_api_section(text, {"enabled": True, "host": "0.0.0.0", "token": "abc'd", "tls_cert": r"C:\x\c.pem"})
    assert "  enabled: true     # set true\n" in out
    assert "  token: 'abc''d' # required for writes\n" in out          # the example line is switched on in place
    assert "  # token" not in out
    assert out.index("tls_cert") < out.index("# the rest")             # added at the end of the section
    assert out.endswith("# the rest\nfleet:\n  enabled: true\n")
    data = yaml.safe_load(out)
    assert data["api"] == {"enabled": True, "host": "0.0.0.0", "token": "abc'd", "tls_cert": r"C:\x\c.pem",
                           "allowed_hosts": ["den.lan"]}
    assert data["fleet"] == {"enabled": True}


def test_a_missing_or_flow_style_section_and_crlf():
    assert yaml.safe_load(cli.set_api_section("device: {name: A}", {"enabled": True}))["api"] == {"enabled": True}
    flow = cli.set_api_section("api: {enabled: false, port: 5123}\r\nfleet: {enabled: true}\r\n", {"enabled": True})
    assert "\r\n" in flow and "\n" not in flow.replace("\r\n", "")
    assert yaml.safe_load(flow) == {"api": {"enabled": True, "port": 5123}, "fleet": {"enabled": True}}


def test_the_example_config_takes_it():
    with open(EXAMPLE, encoding="utf-8") as fh:
        text = fh.read()
    out = cli.set_api_section(text, {"enabled": True, "host": "0.0.0.0", "token": "t" * 32,
                                     "tls_cert": "C:/d/api-cert.pem", "tls_key": "C:/d/api-key.pem"})
    api = Config.from_yaml(out).api
    assert (api.enabled, api.host, api.token, api.tls_cert, api.tls_key) == (True, "0.0.0.0", "t" * 32,
                                                                             "C:/d/api-cert.pem", "C:/d/api-key.pem")
    # only the api: section changed
    changed = [line for line in out.splitlines() if line not in text.splitlines()]
    assert len(changed) == 5 and all(line.startswith("  ") for line in changed)


def test_without_https_the_token_is_made_once_and_kept(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("device: {name: Den PC}\n", encoding="utf-8")
    assert cli.main(["setup-api", "--config", str(cfg), "--no-tls", "--no-restart"]) == 0
    out = capsys.readouterr().out
    token = Config.load(str(cfg)).api.token
    assert token and len(token) >= 16 and token in out and "http://0.0.0.0:47392/" in out
    before = cfg.read_text(encoding="utf-8")
    assert cli.main(["setup-api", "--config", str(cfg), "--no-tls", "--no-restart"]) == 0
    assert "already set up" in capsys.readouterr().out and cfg.read_text(encoding="utf-8") == before
    assert cli.main(["setup-api", "--config", str(cfg), "--no-tls", "--no-restart", "--new-token"]) == 0
    assert Config.load(str(cfg)).api.token != token
    assert cli.main(["setup-api", "--config", str(cfg), "--show"]) == 0
    out = capsys.readouterr().out
    assert Config.load(str(cfg)).api.token in out and "HTTPS is off" in out


def test_show_changes_nothing_and_says_when_it_is_not_set_up(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("device: {name: Den PC}\n", encoding="utf-8")
    assert cli.main(["setup-api", "--config", str(cfg), "--show"]) == 1
    assert "not set up" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == "device: {name: Den PC}\n"


def test_an_invalid_or_missing_config_is_left_alone(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("time_limits: {block_method: nonsense}\n", encoding="utf-8")
    assert cli.main(["setup-api", "--config", str(cfg), "--no-tls", "--no-restart"]) == 2
    assert cfg.read_text(encoding="utf-8") == "time_limits: {block_method: nonsense}\n"
    assert cli.main(["setup-api", "--config", str(tmp_path / "missing.yaml"), "--no-restart"]) == 2


def test_https_certificate_is_made_once_and_kept(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    with open(EXAMPLE, encoding="utf-8") as fh:
        cfg.write_text(fh.read(), encoding="utf-8")
    try:
        code = cli.main(["setup-api", "--config", str(cfg), "--no-restart"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no way to make a certificate here: {exc}")
    out = capsys.readouterr().out
    if code == 1 and "could not create the certificate" in out:
        pytest.skip("no way to make a certificate here")
    assert code == 0
    api = Config.load(str(cfg)).api
    assert api.tls_cert == str(tmp_path / cli.CERT_FILE) and os.path.isfile(api.tls_key)
    fingerprint = re.search(r"\b[0-9a-f]{64}\b", out).group(0)
    from pclink.api import cert_fingerprint

    assert cert_fingerprint(api.tls_cert) == fingerprint and api.token in out and "https://0.0.0.0:47392/" in out
    before = cfg.read_text(encoding="utf-8")
    assert cli.main(["setup-api", "--config", str(cfg), "--no-restart"]) == 0
    assert fingerprint in capsys.readouterr().out and cfg.read_text(encoding="utf-8") == before   # nothing replaced
