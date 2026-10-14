"""The parts of the per-session agent that are pure enough to test anywhere."""
import struct
import sys
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


def test_joystick_needs_real_movement():
    from pclink.agent.win_presence import JOY_AXIS_DEADZONE, joystick_moved

    centre = (32767, 32767, 32767, 32767)
    assert not joystick_moved(centre, (32768, 32767, 32760, 32790))      # jitter across the old bucket boundary
    assert not joystick_moved(centre, (32767 + JOY_AXIS_DEADZONE, 32767, 32767, 32767))
    assert joystick_moved(centre, (32767 + JOY_AXIS_DEADZONE + 1, 32767, 32767, 32767))
    assert joystick_moved((0, 0, 65535, 0), (0, 0, 20000, 0))            # a throttle moved from its rest position


def test_fullscreen_means_exactly_the_monitor():
    from pclink.agent.win_activity import rect_is_fullscreen

    monitor = (0, 0, 1920, 1080)
    assert rect_is_fullscreen((0, 0, 1920, 1080), monitor)
    assert not rect_is_fullscreen((-8, -8, 1928, 1088), monitor)         # maximized, no taskbar on this monitor
    assert not rect_is_fullscreen((0, 0, 1920, 1040), monitor)


def test_close_helpers_refuse_the_wrong_target():
    import os

    from pclink.agent.win_activity import _PROTECTED, close_process, modifiers_block_close

    assert {"pclink.exe", "pclink-agent.exe", "explorer.exe"} <= _PROTECTED
    assert close_process(0, "") == 0
    assert close_process(0, "explorer.exe") == 0
    # a pid whose name does not match is never closed, and never replaced by "everything called <name>"
    assert close_process(os.getpid(), "definitely-not-this-process.exe") == 0
    assert close_process(os.getpid(), "") == 0                           # the agent never terminates itself
    assert modifiers_block_close(True, False, False) and modifiers_block_close(False, True, False)
    assert modifiers_block_close(False, False, True) and not modifiers_block_close(False, False, False)


def test_process_cache_follows_pid_reuse():
    from pclink.agent.win_activity import Win32Activity

    class FakeProc:
        def __init__(self, created, name):
            self._created, self._name = created, name

        def create_time(self):
            return self._created

        def name(self):
            return self._name

        def exe(self):
            return "C:\\" + self._name

    class FakePsutil:
        def __init__(self):
            self.procs = {}

        def Process(self, pid):
            if pid not in self.procs:
                raise RuntimeError("no such process")
            return self.procs[pid]

    act = Win32Activity()
    act._psutil = fake = FakePsutil()
    fake.procs[100] = FakeProc(1.0, "chrome.exe")
    assert act._process(100) == ("chrome.exe", "C:\\chrome.exe")
    fake.procs[100] = FakeProc(2.0, "game.exe")                          # the pid was reused
    assert act._process(100)[0] == "game.exe"
    del fake.procs[100]
    assert act._process(100) == ("", "") and 100 not in act._proc_cache  # failures are not cached
    fake.procs[100] = FakeProc(3.0, "notepad.exe")
    assert act._process(100)[0] == "notepad.exe"


def test_browser_address_bar_and_title_domain():
    from pclink.agent.browser_url import BrowserUrlReader, is_address_bar, looks_like_address

    assert is_address_bar("", "OmniboxViewViews", "")                     # Chromium family
    assert is_address_bar("urlbar-input", "", "")                         # Firefox
    assert is_address_bar("", "", "Address and search bar")
    assert not is_address_bar("searchbox", "", "Search") and not is_address_bar("", "", "")
    assert looks_like_address("example.com/x") and not looks_like_address("version 1.2 notes")
    assert not looks_like_address("hello")
    reader = BrowserUrlReader("title")
    first = reader.url_and_domain(1, "news.example.com - Browser")
    again = reader.url_and_domain(1, "news.example.com - Browser")
    assert first == again and first[1]                                    # first and cached calls agree
    assert reader.url_and_domain(1, "Some page - Browser") == reader.url_and_domain(1, "Some page - Browser")


def test_gsmtc_status_and_backoff():
    from pclink.agent.media import GSMTC_STATUS, gsmtc_backoff

    assert GSMTC_STATUS[4] == "playing" and GSMTC_STATUS[5] == "paused" and GSMTC_STATUS[2] != "playing"
    assert gsmtc_backoff(1) == 30 and gsmtc_backoff(2) == 60 and gsmtc_backoff(50) == 600


def test_tray_reports_whether_it_can_notify():
    from pclink.agent.tray import TrayIcon

    tray = TrayIcon()
    assert tray.notify("t", "m") is False                                 # no window / icon: caller must fall back
    tray._enabled, tray._hwnd, tray._icon_added = True, 1, False
    assert tray.notify("t", "m") is False                                 # Explorer not up: icon was never added


def test_lhm_storage_sensors_stay_between_refreshes():
    from pclink.hwsensors import LhmLibBackend

    class Sensor:
        def __init__(self, name, value):
            self.Name, self.Value, self.SensorType = name, value, "Temperature"

    class Hw:
        def __init__(self, name, hw_type):
            self.Name, self.HardwareType, self.Sensors, self.SubHardware = name, hw_type, [Sensor("Temp", 40.0)], []
            self.updates = 0

        def Update(self):
            self.updates += 1

    backend = LhmLibBackend.__new__(LhmLibBackend)                        # no pythonnet needed for _collect
    cpu, disk = Hw("CPU", "Cpu"), Hw("SSD", "Storage")
    for update_storage in (True, False, False):
        readings, used = [], set()
        for hw in (cpu, disk):
            backend._collect(hw, readings, used, update_storage)
        assert sorted(r.hardware for r in readings) == ["CPU", "SSD"]     # same set on every poll
    assert cpu.updates == 3 and disk.updates == 1                         # storage refreshed only when due


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
    assert s["controller_idle_seconds"] is None
    # off Windows there is no signal; on a real Windows desktop the notification may already have arrived
    assert s["display_state"] in (None, "on", "off", "dimmed")
    flags = (s["screensaver"], s["fullscreen_d3d"], s["display_required"])
    if sys.platform == "win32":
        # a real desktop: a video or a presentation may be holding the display on right now
        assert all(isinstance(flag, bool) for flag in flags)
    else:
        assert flags == (False, False, False)
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
