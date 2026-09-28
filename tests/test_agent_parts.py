"""The parts of the per-session agent that are pure enough to test anywhere."""
import struct
import zlib

from pclink.agent.capture import bgr_rows_to_rgb, encode_image, encode_png
from pclink.agent.media import parse_media_title
from pclink.agent.win_presence import (GUID_CONSOLE_DISPLAY_STATE, GUID_SESSION_DISPLAY_STATE, GUID_SESSION_USER_PRESENCE,
                                       DisplayStateMonitor, PresenceSignals, gamepad_active)
from pclink.models import AgentReport, ForegroundInfo, MediaInfo


def test_gamepad_activity_ignores_resting_pad():
    assert not gamepad_active(0, 0, 0, (100, -200, 3000, 0))       # stick drift inside the dead zone
    assert gamepad_active(0x1000, 0, 0, (0, 0, 0, 0))              # A button
    assert gamepad_active(0, 200, 0, (0, 0, 0, 0))                 # trigger
    assert gamepad_active(0, 0, 0, (0, 20000, 0, 0))               # stick pushed


def test_display_state_monitor_interprets_notifications():
    m = DisplayStateMonitor()
    m.apply(GUID_CONSOLE_DISPLAY_STATE, 0)
    assert m.display_state == "off"
    m.apply(GUID_SESSION_DISPLAY_STATE.lower(), 1)
    assert m.display_state == "on"
    m.apply(GUID_CONSOLE_DISPLAY_STATE, 0)  # console-wide reports lose once a per-session one was seen
    assert m.display_state == "on"
    m.apply(GUID_SESSION_DISPLAY_STATE, 2)
    assert m.display_state == "dimmed"
    m.apply(GUID_SESSION_USER_PRESENCE, 2)
    assert m.user_present is False
    m.apply(GUID_SESSION_USER_PRESENCE, 0)
    assert m.user_present is True


def test_presence_signals_degrade_off_windows():
    p = PresenceSignals(controller=True)
    p.start()
    s = p.sample(42.0)
    assert s["idle_seconds"] == 42.0 and s["input_idle_seconds"] == 42.0
    assert s["controller_idle_seconds"] is None and s["display_state"] is None
    assert s["screensaver"] is False and s["fullscreen_d3d"] is False and s["display_required"] is False
    p.stop()
    rep = AgentReport(session_id=1, ts=0, **{k: v for k, v in s.items()})
    assert AgentReport.from_dict(rep.to_dict()).idle_seconds == 42.0


def test_png_encoder_produces_valid_png():
    width, height = 3, 2
    stride = (width * 3 + 3) & ~3  # 12
    # BGR rows, padded: pixel (255,0,0) in BGR = blue
    row = bytes([255, 0, 0] * width) + b"\0" * (stride - width * 3)
    rgb = bgr_rows_to_rgb(row * height, width, height, stride)
    assert bytes(rgb[:3]) == bytes([0, 0, 255])  # swapped to RGB -> blue is last
    png = encode_png(bytes(rgb), width, height)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png[12:16] == b"IHDR"
    w, h, depth, ctype = struct.unpack("!IIBB", png[16:26])
    assert (w, h, depth, ctype) == (3, 2, 8, 2)
    idat_len = struct.unpack("!I", png[33:37])[0]
    assert png[37:41] == b"IDAT"
    raw = zlib.decompress(png[41:41 + idat_len])
    assert len(raw) == height * (1 + width * 3) and raw[0] == 0
    assert png.endswith(b"IEND" + struct.pack("!I", zlib.crc32(b"IEND") & 0xFFFFFFFF))
    data, fmt = encode_image(bytes(rgb), width, height, "png")
    assert fmt == "png" and data == png


def test_media_title_heuristics():
    m = parse_media_title("spotify.exe", "Daft Punk - Harder, Better, Faster, Stronger")
    assert m.artist == "Daft Punk" and m.title.startswith("Harder") and m.is_playing
    assert parse_media_title("spotify.exe", "Spotify Premium").status == "paused"
    m = parse_media_title("chrome.exe", "(2) Never Gonna Give You Up - YouTube - Google Chrome")
    assert m.title == "Never Gonna Give You Up" and m.app == "YouTube" and m.status == "unknown"
    assert parse_media_title("chrome.exe", "YouTube - Google Chrome") is None
    assert parse_media_title("chrome.exe", "Some article - Google Chrome") is None
    m = parse_media_title("vlc.exe", "movie.mkv - VLC media player")
    assert m.title == "movie.mkv" and m.app == "vlc.exe"
    assert parse_media_title("vlc.exe", "VLC media player").status == "stopped"
    m = parse_media_title("firefox.exe", "Show S01E01 | Netflix — Mozilla Firefox")
    assert m.app == "Netflix" and m.title == "Show S01E01"
    assert parse_media_title("notepad.exe", "todo.txt - Notepad") is None
    assert MediaInfo(title="T", artist="A").label() == "A - T"
    fg = ForegroundInfo(name="spotify.exe", title="X - Y")
    assert fg.name == "spotify.exe"
