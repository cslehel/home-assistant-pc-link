import pytest

from pclink.util import (domain_from_url, human_minutes, parse_hhmm, parse_windows,
                         registrable_domain, slugify, window_status)


@pytest.mark.parametrize("text,expected", [
    ("Kevin", "kevin"),
    ("D:\\", "d"),
    ("Living Room PC!", "living_room_pc"),
    ("", "x"),
    ("Núñez", "nunez"),
])
def test_slugify(text, expected):
    assert slugify(text) == expected


@pytest.mark.parametrize("host,expected", [
    ("www.youtube.com", "youtube.com"),
    ("m.youtube.com", "youtube.com"),
    ("bbc.co.uk", "bbc.co.uk"),
    ("www.bbc.co.uk", "bbc.co.uk"),
    ("a.b.example.com", "example.com"),
    ("localhost", "localhost"),
])
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


@pytest.mark.parametrize("text,expected", [
    ("https://www.youtube.com/watch?v=x", "youtube.com"),
    ("youtube.com/watch", "youtube.com"),
    ("www.reddit.com", "reddit.com"),
    ("edge://settings", None),
    ("about:blank", None),
    ("chrome://flags", None),
    ("just some search text", None),
    ("192.168.1.10:8123/lovelace", "192.168.1.10"),
    ("", None),
    ("mail.google.com", "google.com"),
])
def test_domain_from_url(text, expected):
    assert domain_from_url(text) == expected


def test_domain_from_url_host_group():
    assert domain_from_url("https://m.youtube.com/x", group="host") == "m.youtube.com"


@pytest.mark.parametrize("value,minutes", [("07:00", 420), ("7:30", 450), ("24:00", 1440), ("00:00", 0)])
def test_parse_hhmm(value, minutes):
    assert parse_hhmm(value) == minutes


def test_parse_hhmm_invalid():
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_parse_windows_basic():
    assert parse_windows("07:00-21:00") == [(420, 1260)]
    assert parse_windows(None) == []
    assert parse_windows("all") == []
    assert parse_windows("never") == [(0, 0)]


def test_parse_windows_wraps_midnight():
    # 22:00-02:00 becomes two windows
    assert parse_windows("22:00-02:00") == [(0, 120), (1320, 1440)]


def test_parse_windows_multiple():
    assert parse_windows("07:00-09:00, 16:00-20:00") == [(420, 540), (960, 1200)]
    assert parse_windows(["07:00-08:00", "20:00-21:00"]) == [(420, 480), (1200, 1260)]


def test_window_status_inside_and_outside():
    windows = [(420, 1260)]  # 07:00-21:00
    inside, remaining = window_status(8 * 60, windows)
    assert inside and remaining == pytest.approx(13 * 60)
    inside, remaining = window_status(22 * 60, windows)
    assert not inside and remaining is None


def test_window_status_no_restriction():
    assert window_status(3 * 60, []) == (True, None)


def test_window_status_extends_into_tomorrow():
    # a window ending at midnight, with tomorrow unrestricted, should not warn
    inside, remaining = window_status(23 * 60, [(0, 1440)], next_day_windows=[])
    assert inside
    assert remaining > 60


def test_human_minutes():
    assert human_minutes(None) == "unlimited"
    assert human_minutes(90 * 60) == "1 h 30 min"
    assert human_minutes(5 * 60) == "5 min"
