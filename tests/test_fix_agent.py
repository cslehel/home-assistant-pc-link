"""Review fixes in the agent and in the telemetry / devices / update / GPU / sensor modules.

Only pure logic and fakes: nothing here needs a desktop, a GPU or a browser.
"""
import os
import threading
import time
import types

import pytest

from pclink.config import TelemetryConfig, WindowsUpdateConfig


# -- telemetry ---------------------------------------------------------------------------------

def test_top_processes_leave_out_the_idle_process():
    from pclink.telemetry import group_processes

    rows = [(0, "System Idle Process", 760.0, 8192), (4, "System", 8.0, 1 << 20),
            (10, "game.exe", 160.0, 900 << 20), (11, "game.exe", 40.0, 100 << 20), (12, "chrome.exe", 16.0, 2000 << 20)]
    top = group_processes(rows, ncpu=8, n=2)
    assert top["top_process"] == "game.exe" and top["top_process_cpu"] == 25.0
    assert [g["name"] for g in top["top_processes"]["by_cpu"]] == ["game.exe", "chrome.exe"]
    assert top["top_memory_process"] == "chrome.exe" and top["top_memory_process_mb"] == 2000
    assert all("idle" not in g["name"] for g in top["top_processes"]["by_cpu"] + top["top_memory_processes"]["by_memory"])
    empty = group_processes([], 1, 3)
    assert empty["top_process"] is None and empty["top_processes"]["by_cpu"] == []


def test_primary_interface_is_the_one_with_the_default_route():
    from pclink.telemetry import choose_primary, directed_broadcast

    nics = [
        {"name": "VMware Network Adapter VMnet1", "mac": "00:50:56:C0:00:01", "ipv4": "192.168.171.1", "up": True},
        {"name": "Wi-Fi", "mac": "AA:BB:CC:DD:EE:FF", "ipv4": "192.168.1.55", "up": True},
        {"name": "Ethernet", "mac": "11:22:33:44:55:66", "ipv4": "169.254.3.4", "up": False},
    ]
    assert choose_primary(nics, "192.168.1.55")["name"] == "Wi-Fi"          # sorting by name used to pick VMware
    assert choose_primary(nics, None)["name"] == "Wi-Fi"                    # offline: a physical adapter first
    assert choose_primary(nics, "10.9.9.9")["name"] == "Wi-Fi"              # a route through an unlisted (VPN) adapter
    assert choose_primary([], "192.168.1.55") is None
    assert directed_broadcast("192.168.1.55", "255.255.255.0") == "192.168.1.255"
    assert directed_broadcast("10.1.2.3", "255.255.240.0") == "10.1.15.255"
    assert directed_broadcast("192.168.1.55", None) is None and directed_broadcast(None, "255.255.255.0") is None
    assert directed_broadcast("192.168.1.55", "255.255.255.255") is None    # a point-to-point link has none
    assert directed_broadcast("not-an-ip", "255.255.255.0") is None


def test_telemetry_snapshot_reports_broadcast_and_never_scans_processes(monkeypatch):
    from pclink import telemetry

    monkeypatch.setattr(telemetry, "default_route_ip", lambda: None)
    cfg = TelemetryConfig()
    cfg.top_processes = 3
    t = telemetry.Telemetry(cfg)
    called = []
    monkeypatch.setattr(t, "top_processes", lambda n: called.append(threading.current_thread().name) or {"top_process": "x"})
    # the reader thread is not started here: the snapshot must work from the cached value alone
    t._top._thread = threading.current_thread()
    snap = t.snapshot()
    assert called == []                                # nothing scanned on the caller's thread
    assert snap["top_process"] is None and "broadcast" in snap and "mac" in snap
    t._top.refresh_now()                               # what the worker does
    assert t.snapshot()["top_process"] == "x"
    t.stop()


def test_background_value_keeps_the_last_good_result():
    from pclink.telemetry import Background

    results = iter([1, RuntimeError("boom"), 3])

    def fn():
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    bg = Background("t", fn, lambda: 0.01, initial=0)
    bg._thread = threading.current_thread()            # no worker: refreshed by hand
    assert bg.value() == 0
    bg.refresh_now()
    assert bg.value() == 1
    bg.refresh_now()                                   # a failure changes nothing
    assert bg.value() == 1 and bg.errors == 1
    bg.refresh_now()
    assert bg.value() == 3 and bg.errors == 0


def test_background_worker_refreshes_without_blocking_the_reader():
    from pclink.telemetry import Background

    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.3)
        return "done"

    bg = Background("slow", slow, lambda: 30.0, initial="old", min_interval=0.05)
    t0 = time.monotonic()
    assert bg.value() == "old" and time.monotonic() - t0 < 0.2    # returns at once while the worker runs
    assert started.wait(2)
    assert bg.value(wait_first=3) == "done"
    bg.stop()


# -- GPU ---------------------------------------------------------------------------------------

def test_gpu_engines_are_aggregated_per_adapter():
    from pclink.gpu import aggregate_gpu_engines, busiest

    values = {
        "pid_1_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D": 30.0,
        "pid_2_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D": 25.0,
        "pid_2_luid_0x00000000_0x0000AAAA_phys_0_eng_5_engtype_VideoDecode": 20.0,
        "pid_3_luid_0x00000000_0x0000BBBB_phys_0_eng_0_engtype_3D": 60.0,     # the other adapter
        "pid_3_luid_0x00000000_0x0000BBBB_phys_0_eng_1_engtype_Copy": 70.0,
        "garbage": 99.0,
    }
    gpus = aggregate_gpu_engines(values)
    assert [(g["load"], g["engine"]) for g in gpus] == [(55.0, "3D"), (70.0, "Copy")]
    assert busiest(gpus)["load"] == 70.0                                      # never 55 + 60: two GPUs are not one
    assert aggregate_gpu_engines({"pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D": 250.0})[0]["load"] == 100.0
    assert aggregate_gpu_engines({}) == [] and busiest([]) is None
    # engine type names are kept whole ("VideoDecode" used to lose its last letter)
    assert aggregate_gpu_engines({"pid_1_luid_0x0_0x1_phys_0_eng_2_engtype_VideoDecode": 1.0})[0]["engine"] == "VideoDecode"


class FakePdh:
    PDH_FMT_DOUBLE = 0x200

    def __init__(self):
        self.added, self.english, self.values, self.collects, self.closed = [], [], {}, 0, 0

    def OpenQuery(self):
        return object()

    def AddCounter(self, query, path):
        self.added.append(path)
        return path

    def AddEnglishCounter(self, query, path):
        self.english.append(path)
        return path

    def CollectQueryData(self, query):
        self.collects += 1

    def GetFormattedCounterArray(self, counter, fmt):
        return dict(self.values.get(counter, {}))

    def CloseQuery(self, query):
        self.closed += 1


def test_pdh_gpu_uses_one_english_wildcard_counter_and_sees_new_processes():
    from pclink.gpu import PdhGpu

    pdh = FakePdh()
    gpu = PdhGpu(pdh)
    assert pdh.english == [PdhGpu.ENGINE_PATH, PdhGpu.MEMORY_PATH] and pdh.added == []   # works on every display language
    assert "(*)" in PdhGpu.ENGINE_PATH
    pdh.values[PdhGpu.ENGINE_PATH] = {"pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D": 10.0}
    assert gpu.read()["load"] == 10.0
    # a game that started just now is in the very next reading: nothing has to be rebuilt
    pdh.values[PdhGpu.ENGINE_PATH]["pid_9_luid_0x0_0x1_phys_0_eng_0_engtype_3D"] = 75.0
    pdh.values[PdhGpu.MEMORY_PATH] = {"luid_0x0_0x1_phys_0": 512 * 1024 * 1024}
    reading = gpu.read()
    assert reading["load"] == 85.0 and reading["memory_used_mb"] == 512
    assert pdh.closed == 0
    pdh.values[PdhGpu.ENGINE_PATH] = {}
    assert gpu.read()["load"] == 85.0 and gpu.read() is None     # one gap is bridged, a lasting one is not


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0

    class NVMLError(Exception):
        pass

    def __init__(self, loads):
        self.loads, self.broken, self.inits, self.shutdowns = loads, False, 0, 0

    def nvmlInit(self):
        if self.broken:
            raise self.NVMLError("driver not loaded")
        self.inits += 1

    def nvmlShutdown(self):
        self.shutdowns += 1

    def nvmlDeviceGetCount(self):
        return len(self.loads)

    def nvmlDeviceGetHandleByIndex(self, i):
        return i

    def nvmlDeviceGetName(self, h):
        return f"GPU {h}".encode()

    def _check(self):
        if self.broken:
            raise self.NVMLError("GPU is lost")

    def nvmlDeviceGetUtilizationRates(self, h):
        self._check()
        return types.SimpleNamespace(gpu=self.loads[h])

    def nvmlDeviceGetMemoryInfo(self, h):
        self._check()
        return types.SimpleNamespace(used=1 << 30, total=4 << 30)

    def nvmlDeviceGetTemperature(self, h, which):
        self._check()
        return 50 + h

    def nvmlDeviceGetPowerUsage(self, h):
        self._check()
        return 90000


def test_nvml_reads_every_gpu_and_recovers_after_a_driver_update(monkeypatch):
    from pclink.gpu import GpuReader, NvmlGpu

    nv = FakeNvml([5, 80])
    gpu = NvmlGpu(nv)
    reading = gpu.read()
    assert reading["name"] == "GPU 1" and reading["load"] == 80.0 and reading["temperature"] == 51.0
    assert [g["name"] for g in reading["gpus"]] == ["GPU 0", "GPU 1"]         # the second card is no longer ignored
    nv.broken = True
    for _ in range(NvmlGpu.FAILURES_BEFORE_REINIT):
        assert gpu.read() is None
    assert nv.shutdowns == 1 and gpu._handles == []
    nv.broken = False
    assert gpu.read() is None                                                 # not hammered: waits before trying again
    gpu._retry_at = 0.0
    assert gpu.read()["load"] == 80.0 and nv.inits == 2                       # back without a service restart

    class Pdh:
        name = "counters"

        def read(self):
            return {"name": "counters", "load": 33.0}

        def close(self):
            pass

    nv.broken = True
    assert GpuReader(gpu, Pdh()).read()["load"] == 33.0                       # the counters fill in meanwhile


# -- devices ------------------------------------------------------------------------------------

def test_empty_card_reader_slots_are_not_counted():
    from pclink.devices import drive_entry

    assert drive_entry("C:\\", "NTFS", "rw,fixed", (100, 50)) is None
    stick = drive_entry("E:\\", "FAT32", "rw,removable", (32 * 1024 ** 3, 16 * 1024 ** 3))
    assert stick["media"] is True and stick["total_gb"] == 32.0 and stick["kind"] == "removable"
    slot = drive_entry("F:\\", "", "rw,removable", None)
    assert slot["media"] is False and slot["fstype"] is None
    assert drive_entry("G:\\", "", "ro,cdrom", None) == {"mount": "G:\\", "fstype": None, "kind": "cdrom", "media": False}


def test_device_snapshot_is_read_from_the_worker_result(monkeypatch):
    from pclink import devices

    calls = []
    monkeypatch.setattr(devices, "usb_devices", lambda: calls.append("usb") or [
        {"name": "Mouse", "class": "HIDClass", "instance_id": "USB\\1", "vid": None, "pid": None, "hub": False}])
    monkeypatch.setattr(devices, "removable_drives", lambda: [
        {"mount": "E:\\", "media": True}, {"mount": "F:\\", "media": False}, {"mount": "G:\\"}])
    monkeypatch.setattr(devices, "printers", lambda: {"printers": {}, "printer_status": "none", "print_jobs": 0})
    cfg = TelemetryConfig()
    cfg.usb_devices = cfg.printers = True
    events = []
    mon = devices.DeviceMonitor(cfg, on_event=lambda kind, data: events.append(kind))
    mon._worker._thread = threading.current_thread()   # no worker thread in this test
    mon.FIRST_RESULT_SECONDS = 0.0
    assert mon.snapshot() == {} and calls == []        # the caller never enumerates anything itself
    mon._worker.refresh_now()
    snap = mon.snapshot()
    assert snap["usb_devices"] == 1 and snap["removable_drives"] == 2 and snap["printer_status"] == "none"
    assert len(snap["removable_drive_list"]["drives"]) == 3      # listed, only not counted
    monkeypatch.setattr(devices, "usb_devices", lambda: [])
    mon._usb_at = -1e9
    mon._worker.refresh_now()
    assert mon.snapshot()["usb_devices"] == 1 and events == []   # one empty enumeration is not believed
    mon._usb_at = -1e9
    mon._worker.refresh_now()
    assert mon.snapshot()["usb_devices"] == 0 and events == ["usb_changed"]
    mon.stop()


# -- Windows Update ------------------------------------------------------------------------------

def test_pending_file_renames_alone_are_no_reason_to_restart():
    from pclink.winupdate import reboot_verdict

    assert reboot_verdict(["pending file renames"]) == (False, [])
    assert reboot_verdict([]) == (False, [])
    assert reboot_verdict(["windows update", "pending file renames"]) == (True, ["windows update", "pending file renames"])
    assert reboot_verdict(["component servicing"])[0] is True


def _update(allow=True):
    from pclink.winupdate import WindowsUpdate

    cfg = WindowsUpdateConfig()
    cfg.allow_install = allow
    return WindowsUpdate(cfg)


def test_update_install_does_not_block_and_reports_honestly(monkeypatch):
    from pclink import winupdate

    monkeypatch.setattr(winupdate, "_IS_WIN", False)
    assert _update(allow=False).install() is False

    wu = _update()
    gate, verbs = threading.Event(), []

    def slow(verb):
        verbs.append(verb)
        gate.wait(5)
        return True

    monkeypatch.setattr(wu, "_uso", slow)
    t0 = time.monotonic()
    assert wu.install() is True and time.monotonic() - t0 < 1.0             # the caller (the main loop) is not held
    assert wu.snapshot()["update_details"]["status"] != "installing"         # not before UsoClient really started
    gate.set()
    deadline = time.monotonic() + 5
    while wu.snapshot()["update_details"]["status"] != "installing" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert wu.snapshot()["update_details"]["status"] == "installing" and verbs == ["StartDownload", "StartInstall"]
    assert wu._recheck_at is not None

    failed = _update()
    monkeypatch.setattr(failed, "_uso", lambda verb: verb == "StartDownload")
    assert failed._install() is False
    details = failed.snapshot()["update_details"]
    assert details["status"] != "installing" and details["error"]
    assert failed._recheck_at is None


def test_usoclient_exit_code_counts(monkeypatch):
    from pclink import winupdate

    monkeypatch.setattr(winupdate, "_IS_WIN", True)
    codes = iter([0, 1])
    seen = []

    def run(args, **kwargs):
        seen.append((args[-1], kwargs.get("timeout")))
        return types.SimpleNamespace(returncode=next(codes))

    monkeypatch.setattr(winupdate.subprocess, "run", run)
    wu = _update()
    assert wu._uso("StartScan") is True and wu._uso("StartInstall") is False
    assert seen == [("StartScan", 60), ("StartInstall", 60)]

    def boom(args, **kwargs):
        raise winupdate.subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(winupdate.subprocess, "run", boom)
    assert wu._uso("StartScan") is False


def test_update_search_does_not_change_a_snapshot_already_handed_out(monkeypatch):
    from pclink import winupdate

    monkeypatch.setattr(winupdate, "_IS_WIN", True)

    def no_network(client):
        raise RuntimeError("no network")

    monkeypatch.setattr(winupdate.WindowsUpdate, "_query_updates", staticmethod(no_network))   # never a real search
    wu = _update()
    wu._reboot_checked = time.monotonic()               # no registry in this test
    before = wu.snapshot()
    details = before["update_details"]
    wu._search()                                        # fails (here, or for want of pywin32); a new dict is stored
    assert details["status"] == "unknown" and before["update_details"] is details
    after = wu.snapshot()["update_details"]
    assert after is not details and after["status"] == "error" and after["error"]


# -- hardware sensors -----------------------------------------------------------------------------

def test_sensor_group_captions_map_to_types():
    from pclink.hwsensors import group_sensor_type, image_hardware_type

    for caption, expected in (("Temperatures", "temperature"), ("Fans", "fan"), ("Load", "load"), ("Powers", "power"),
                              ("Frequencies", "frequency"), ("Energies", "energy"), ("Small Data", "smalldata"),
                              ("Noise Levels", "noise"), ("Data", "data"), ("Throughput", "throughput"),
                              ("Voltages", "voltage"), ("Controls", "control"), ("Levels", "level")):
        assert group_sensor_type(caption) == expected, caption
    assert group_sensor_type("Widgets") == "widget"                     # unknown plurals still lose their "s"
    assert group_sensor_type("Whatever", "SmallData") == "smalldata"    # the sensor's own type wins
    assert image_hardware_type("images_icon/nvidia.png") == "gpu" and image_hardware_type("images_icon/cpu.png") == "cpu"
    assert image_hardware_type("images_icon/hdd.png") == "storage" and image_hardware_type("") == ""


def test_lhm_http_tree_is_read_with_the_right_types():
    from pclink.hwsensors import LhmHttpBackend

    backend = LhmHttpBackend.__new__(LhmHttpBackend)
    hw = {"Text": "Board", "ImageURL": "images_icon/mainboard.png", "Children": [
        {"Text": "Frequencies", "Value": "", "Children": [{"Text": "Bus", "Value": "100,0 Hz", "Children": []}]},
        {"Text": "Temperatures", "Value": "", "Children": [{"Text": "CPU", "Value": "45.5 °C", "Type": "Temperature", "Children": []},
                                                           "not a sensor"]},
    ]}
    readings, used = [], set()
    backend._walk_hw(hw, readings, used)
    assert [(r.sensor_type, r.value, r.hardware_type) for r in readings] == [("frequency", 100.0, "motherboard"),
                                                                             ("temperature", 45.5, "motherboard")]


def test_slow_sensor_reads_do_not_make_the_readings_stale():
    from pclink.hwsensors import read_pause, stale_after

    assert read_pause(10, 0.5) == 10 and read_pause(10, 20) == 80 and read_pause(10, 500) == 300
    for elapsed in (0.1, 5, 16, 40, 120):
        pause = read_pause(10, elapsed)
        # the readings stay valid until the next read (the wait plus that read) has finished
        assert stale_after(10, pause) > pause + elapsed
    assert stale_after(10, 10) == 60 and stale_after(30, 30) == 120


# -- agent: foreground / closing -------------------------------------------------------------------

def test_close_requests_check_the_creation_time():
    from pclink.agent.win_activity import close_process, created_matches

    assert created_matches(0.0, 1234.5)                      # the service did not say: only the name is checked
    assert created_matches(1000.0, 1000.6) and created_matches(1000.0, 999.4)
    assert not created_matches(1000.0, 1002.0)               # the pid was reused
    assert not created_matches(1000.0, 0.0)
    # this very process, right name, wrong creation time: refused before anything else is looked at
    assert close_process(os.getpid(), "", 1.0) == 0


def test_agent_never_closes_itself_its_parent_or_the_shell(monkeypatch):
    from pclink.agent import win_activity as w
    from pclink.rules import NEVER_CLOSE

    assert NEVER_CLOSE <= w._PROTECTED                        # the agent refuses at least what the service refuses
    assert {"pclink.exe", "pclink-agent.exe"} <= w._PROTECTED
    assert {os.getpid(), os.getppid()} <= w.protected_pids()
    monkeypatch.setenv("PCLINK_SERVICE_PID", "4242")
    assert 4242 in w.protected_pids()
    monkeypatch.setenv("PCLINK_SERVICE_PID", "nonsense")
    assert os.getpid() in w.protected_pids()
    assert w.close_process(os.getppid(), "") == 0             # whatever it is called
    for name in ("explorer.exe", "ApplicationFrameHost.exe", "LogonUI.exe", "pclink.exe"):
        assert w.close_process(0, name) == 0


def test_uac_prompt_is_not_a_locked_session():
    from pclink.agent.win_activity import injection_blocked, secure_desktop_means_locked

    assert not secure_desktop_means_locked("Default", None)
    assert secure_desktop_means_locked("Winlogon", True) and secure_desktop_means_locked(None, True)
    assert not secure_desktop_means_locked("Winlogon", False)       # a UAC prompt on an unlocked session
    assert not secure_desktop_means_locked(None, False)
    assert secure_desktop_means_locked("Winlogon", None)            # unknown: as before, locked
    assert secure_desktop_means_locked("Screen-saver", False)
    assert injection_blocked(False, True) and injection_blocked(None, True)
    assert not injection_blocked(True, True) and not injection_blocked(False, False) and not injection_blocked(False, None)


def test_foreground_reports_the_creation_time():
    from pclink.agent.win_activity import Win32Activity

    class Proc:
        def create_time(self):
            return 1700000000.25

        def name(self):
            return "game.exe"

        def exe(self):
            return "C:\\game.exe"

    act = Win32Activity()
    act._psutil = types.SimpleNamespace(Process=lambda pid: Proc())
    assert act._process_info(7) == ("game.exe", "C:\\game.exe", 1700000000.25)
    assert act._process(7) == ("game.exe", "C:\\game.exe")
    assert act._process_info(0) == ("", "", 0.0)


# -- agent: presence ---------------------------------------------------------------------------------

def test_joystick_buttons_count_when_they_change():
    from pclink.agent.win_presence import joystick_used

    rest = ((32767, 32767, 0, 0), 65535, 0b100)                  # a switch that is always "pressed"
    assert not joystick_used(None, rest[0], rest[1], rest[2])    # the first look only records
    assert not joystick_used(rest, rest[0], rest[1], rest[2])    # held for ever: not input
    assert joystick_used(rest, rest[0], rest[1], 0b101)          # a button went down
    assert joystick_used(rest, rest[0], rest[1], 0)              # ... or up
    assert joystick_used(rest, (60000, 32767, 0, 0), rest[1], rest[2])
    assert joystick_used(rest, rest[0], 9000, rest[2])


def test_machine_wide_signals_need_this_seat_to_agree_on_multiseat():
    from pclink.agent import win_presence as p

    assert p.machine_wide_counts(False, False, False, False)             # one seat: the signal is ours
    assert not p.machine_wide_counts(True, False, False, False)          # another seat may be the one watching a film
    assert p.machine_wide_counts(True, True, False, False) and p.machine_wide_counts(True, False, True, False)
    assert p.machine_wide_counts(True, False, False, True)
    signals = p.PresenceSignals(controller=False)
    signals._other_seats, signals._seats_checked = True, time.monotonic()
    assert signals.sample(5.0)["display_required"] is False              # whatever the machine-wide state says
    assert signals.controller.counts is False


def test_toasts_are_known_to_be_suppressed():
    from pclink.agent.win_presence import (QUNS_ACCEPTS_NOTIFICATIONS, QUNS_BUSY, QUNS_NOT_PRESENT, QUNS_PRESENTATION_MODE,
                                           QUNS_QUIET_TIME, QUNS_RUNNING_D3D_FULL_SCREEN, toast_suppressed)

    for state in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME):
        assert toast_suppressed(state)
    assert not toast_suppressed(QUNS_ACCEPTS_NOTIFICATIONS) and not toast_suppressed(None)
    assert not toast_suppressed(QUNS_NOT_PRESENT)


# -- agent: screenshots --------------------------------------------------------------------------------

def test_screenshot_is_shrunk_until_it_fits():
    from pclink.agent.capture import MIN_JPEG_QUALITY, fit_capture, image_budget

    assert image_budget(8 * 1024 * 1024) < 6 * 1024 * 1024               # base64 and the rest of the message fit
    assert (image_budget(8 * 1024 * 1024) * 4 // 3) + 4096 < 8 * 1024 * 1024
    grabs, encodes = [], []

    def grab(width):
        grabs.append(width)
        return b"", width, width // 2

    def jpeg(rgb, w, h, fmt, quality):
        encodes.append((w, quality))
        return b"x" * (w * h * quality // 100), "jpeg"

    result = fit_capture(grab, jpeg, 400_000, 1920, "jpeg", 60)
    assert len(result["data"]) <= 400_000 and result["format"] == "jpeg"
    assert encodes[0] == (1920, 60) and encodes[1][1] < 60               # quality goes first: no new capture needed
    assert grabs[0] == 1920 and (len(grabs) == 1 or encodes[-1][1] == MIN_JPEG_QUALITY)

    def png(rgb, w, h, fmt, quality):
        return b"x" * (w * h * 3), "png"

    grabs.clear()
    result = fit_capture(grab, png, 600_000, 3840, "png", 60)
    assert len(result["data"]) <= 600_000 and result["width"] < 3840 and len(grabs) >= 2     # taken again, smaller
    small = fit_capture(grab, png, 10 ** 9, 1280, "png", 60)
    assert small["width"] == 1280                                        # what fits is left alone
    failed = fit_capture(grab, png, 1000, 1280, "png", 60)
    assert "error" in failed and "does not fit" in failed["error"] and "data" not in failed


# -- agent: browser address ------------------------------------------------------------------------------

def test_url_cache_rules():
    from pclink.agent.browser_url import (MIN_WALK_SECONDS, READ_NONE, READ_TYPING, READ_URL, RECHECK_SECONDS, UrlCache)

    c = UrlCache()
    assert c.due(1, "Home", 0.0)
    c.asked(1, "Home", 0.0)
    c.record(1, "Home", READ_URL, "school.example/home")
    assert c.current(1, "Home") == "school.example/home"
    assert not c.due(1, "Home", RECHECK_SECONDS - 1)
    assert c.due(1, "Home", RECHECK_SECONDS)                             # same title: looked at again all the same
    c.asked(1, "Home", RECHECK_SECONDS)
    c.record(1, "Home", READ_URL, "games.example/home")                  # another site with the same title
    assert c.current(1, "Home") == "games.example/home"

    # a new title in the same window: nothing is reported until it was read, and it is not read too often
    t = RECHECK_SECONDS + 1
    assert c.current(1, "Other") is None and not c.due(1, "Other", t)
    assert c.due(1, "Other", RECHECK_SECONDS + MIN_WALK_SECONDS)
    assert c.due(2, "Other", t)                                          # another window: at once

    # a failed read is not final, and does not wipe what is known for the same page
    t = 100.0
    c.asked(1, "Home", t)
    c.record(1, "Home", READ_NONE)
    assert c.current(1, "Home") == "games.example/home"
    assert not c.due(1, "Home", t + 1) and c.due(1, "Home", t + MIN_WALK_SECONDS)

    # typing: the committed address stays, the typed text is never taken
    c.asked(1, "Home", 200.0)
    c.record(1, "Home", READ_TYPING)
    assert c.current(1, "Home") == "games.example/home" and not c.due(1, "Home", 200.0 + MIN_WALK_SECONDS)
    c.asked(1, "New Tab", 300.0)
    c.record(1, "New Tab", READ_TYPING)
    assert c.current(1, "New Tab") is None                               # nothing committed for this page yet
    assert c.due(1, "New Tab", 300.0 + MIN_WALK_SECONDS)


def test_toolbar_search_never_enters_the_page():
    from pclink.agent.browser_url import BrowserUrlReader, toolbar_step

    assert toolbar_step(50004, "", "OmniboxViewViews", "") == "match"
    assert toolbar_step(50003, "urlbar-input", "", "") == "match"                  # Firefox: a combo box
    assert toolbar_step(50004, "", "", "Address and search bar") == "match"
    assert toolbar_step(50030, "", "Chrome_RenderWidgetHostHWND", "Some page") == "skip"   # the document
    assert toolbar_step(50004, "q", "", "Search") == "skip"                        # a text field of the page
    assert toolbar_step(50019, "", "", "A tab") == "skip" and toolbar_step(50000, "", "", "Back") == "skip"
    for container in (50033, 50032, 50021, 50026, 50025):
        assert toolbar_step(container, "", "", "") == "expand"
    assert toolbar_step(None, "", "", "") == "skip"

    class Element:
        def __init__(self, ctype, cls="", name="", children=()):
            self.props = {30003: ctype, 30012: cls, 30005: name, 30011: ""}
            self.children, self.opened = list(children), 0

        def GetCachedPropertyValue(self, prop_id):
            return self.props[prop_id]

        def FindAllBuildCache(self, scope, condition, request):
            assert scope == 2                                                       # children only, never descendants
            self.opened += 1
            kids = self.children
            return types.SimpleNamespace(Length=len(kids), GetElement=lambda i: kids[i])

    bar = Element(50004, "OmniboxViewViews")
    page = Element(50030, "", "Page", [Element(50004, "", "Address bar")])          # a look-alike inside the page
    root = Element(50032, "", "Browser", [page, Element(50033, "", "", [Element(50021, "", "", [Element(50000), bar])])])
    uia = types.SimpleNamespace(CreateCacheRequest=lambda: types.SimpleNamespace(AddProperty=lambda p: None),
                                CreateTrueCondition=lambda: object())
    assert BrowserUrlReader._toolbar_search(uia, root) is bar and page.opened == 0
    assert BrowserUrlReader._toolbar_search(uia, Element(50032, "", "", [page])) is None and page.opened == 0


def test_reader_reports_the_full_host_and_survives_a_hanging_browser(monkeypatch):
    from pclink.agent import browser_url as b

    assert b.BrowserUrlReader.host_of("https://Mail.Example.co.uk/inbox") == "mail.example.co.uk"
    assert b.BrowserUrlReader.host_of("www.example.com/x") == "www.example.com"
    assert b.BrowserUrlReader.host_of("how to cook rice") is None and b.BrowserUrlReader.host_of(None) is None

    reader = b.BrowserUrlReader("title")
    reader.method = "uia"
    hang = threading.Event()
    answers = [(b.READ_URL, "a.example.com/1")]

    def read(uia, hwnd):
        if not answers:
            hang.wait(10)
            return b.READ_NONE, None
        return answers.pop(0)

    monkeypatch.setattr(reader, "_create_uia", lambda: object())
    monkeypatch.setattr(reader, "_read_omnibox", read)
    monkeypatch.setattr(b, "FAST_WAIT_SECONDS", 2.0)
    assert reader.url_and_domain(5, "Page A") == ("a.example.com/1", "example.com")
    monkeypatch.setattr(b, "FAST_WAIT_SECONDS", 0.05)
    t0 = time.monotonic()
    assert reader.url_and_domain(6, "Page B")[0] is None and time.monotonic() - t0 < 1.0     # the poll is not held
    stuck = reader._worker
    assert stuck.busy
    stuck.busy_since -= b.ABANDON_SECONDS + 1
    reader.url_and_domain(6, "Page B")
    assert stuck.abandoned and reader._worker is not stuck and reader._abandoned == 1        # given up, a new reader took over
    hang.set()


# -- agent: tray and notifications ------------------------------------------------------------------------

def test_balloons_are_queued_not_overwritten():
    from pclink.agent.tray import BALLOON_GAP_SECONDS, TrayIcon, balloon_wait

    assert balloon_wait(10.0, 0.0) == 0.0 and balloon_wait(10.0, 9.0) == BALLOON_GAP_SECONDS - 1.0
    assert balloon_wait(5.0, 9.0) == 0.0
    tray = TrayIcon()
    tray._enabled, tray._hwnd, tray._icon_added = True, 1, True
    tray._WM_BALLOON = 0x400 + 22
    tray.notify("Time", "5 minutes left", "warning")
    tray.notify("PC Link", "A parent took a screenshot of this screen.", "info")
    assert [b[1] for b in tray._balloons] == ["5 minutes left", "A parent took a screenshot of this screen."]
    shown, timers = [], []
    tray._show_balloon = shown.append
    tray._set_timer = lambda ident, ms: timers.append(ms) or True
    tray._drain_balloons()
    assert [b[1] for b in shown] == ["5 minutes left"] and len(timers) == 1                 # the second one waits its turn
    tray._balloon_shown_at -= BALLOON_GAP_SECONDS + 1
    tray._drain_balloons()
    assert [b[1] for b in shown] == ["5 minutes left", "A parent took a screenshot of this screen."]
    assert not tray._balloons


def test_warnings_reach_a_user_in_a_full_screen_game():
    from pclink.agent.main import (VIA_BALLOON, VIA_BOX, VIA_NONE, VIA_QUIET, message_box_style, notify_outcome, notify_plan)

    assert notify_plan("info", True, False) == (True, False)
    assert notify_plan("warning", True, False) == (True, False)              # a toast is enough when it is shown
    assert notify_plan("warning", True, True) == (True, True)                # full-screen game: also a box on top
    assert notify_plan("critical", True, True) == (True, True)
    assert notify_plan("info", True, True) == (True, False)                  # an info message does not interrupt a game
    assert notify_plan("info", False, False) == (False, True)                # no tray icon: the box is all there is
    assert notify_outcome(True, False, False) == VIA_BALLOON and notify_outcome(True, True, True) == VIA_BOX
    assert notify_outcome(True, False, True) == VIA_QUIET                    # honest: it is in the notification centre only
    assert notify_outcome(False, False, False) == VIA_NONE
    MB_SYSTEMMODAL, MB_TOPMOST, MB_SETFOREGROUND = 0x1000, 0x40000, 0x10000
    assert message_box_style("critical") & MB_SYSTEMMODAL
    assert message_box_style("warning") & MB_TOPMOST and not message_box_style("warning") & MB_SYSTEMMODAL
    assert not any(message_box_style(level) & MB_SETFOREGROUND for level in ("info", "warning", "critical"))


def _bare_agent():
    """An Agent without its Windows parts, for the logic around them."""
    from pclink.agent.main import Agent

    agent = Agent.__new__(Agent)
    agent.sent, agent.tray_states = [], []
    agent.client = types.SimpleNamespace(connected=threading.Event(), send=lambda m: agent.sent.append(m) or True)
    from pclink.agent.tray import RequestGuard

    agent.offers, agent.balloons = [], []
    agent.tray = types.SimpleNamespace(update=lambda state, tooltip=None, detail=None: agent.tray_states.append((state, detail)),
                                       can_notify=True, notify=lambda *a: agent.balloons.append(a) or True,
                                       set_time_requests=agent.offers.append)
    agent._request_guard = RequestGuard()
    agent.presence = types.SimpleNamespace(last_quns=None)
    agent.activity = types.SimpleNamespace(last_created=1700000000.5)
    agent._tray_connected, agent._last_status, agent._status_text = False, None, "Connecting..."
    agent._boxes_open, agent._boxes_lock = 0, threading.Lock()
    return agent


def test_tray_never_stays_on_connecting_once_connected():
    agent = _bare_agent()
    agent.client.connected.set()
    agent._update_tray_connection()
    assert agent.tray_states == [("ok", "Connected")]                        # even if the service sends nothing
    agent._update_tray_connection()
    assert len(agent.tray_states) == 1                                       # only on a change, not every poll
    agent._on_message({"type": "status", "text": "42 min left today", "remaining_minutes": 42})
    assert agent.tray_states[-1] == ("ok", "42 min left today")
    agent.client.connected.clear()
    agent._update_tray_connection()
    assert agent.tray_states[-1][0] == "offline"
    agent.client.connected.set()
    agent._update_tray_connection()                                          # reconnected, the status did not change
    assert agent.tray_states[-1] == ("ok", "42 min left today")
    fresh = _bare_agent()
    fresh._on_message({"type": "hello_ok"})                                  # forwarded by the IPC client
    assert fresh.tray_states == [("ok", "Connected")]


def test_notify_answers_the_service(monkeypatch):
    from pclink.agent import main
    from pclink.agent.win_presence import QUNS_RUNNING_D3D_FULL_SCREEN

    agent = _bare_agent()
    boxes = []
    monkeypatch.setattr(agent, "_message_box", lambda title, message, level: boxes.append(level) or True)
    agent._on_message({"type": "notify", "id": "n1", "title": "T", "message": "M", "level": "warning"})
    assert agent.sent[-1] == {"type": "notified", "id": "n1", "shown": True, "via": main.VIA_BALLOON} and boxes == []
    agent.presence.last_quns = QUNS_RUNNING_D3D_FULL_SCREEN
    agent._on_message({"type": "notify", "id": "n2", "title": "T", "message": "5 minutes left", "level": "warning"})
    assert agent.sent[-1]["via"] == main.VIA_BOX and boxes == ["warning"]
    agent._on_message({"type": "notify", "id": "n3", "title": "T", "message": "break?", "level": "info"})
    assert agent.sent[-1] == {"type": "notified", "id": "n3", "shown": False, "via": main.VIA_QUIET}
    monkeypatch.setattr(agent, "_message_box", lambda *a: False)             # too many boxes open already
    agent.tray.can_notify = False
    agent._on_message({"type": "notify", "id": "n4", "title": "T", "message": "M", "level": "critical"})
    assert agent.sent[-1] == {"type": "notified", "id": "n4", "shown": False, "via": main.VIA_NONE}   # the service falls back


def test_report_carries_host_agent_pid_and_creation_time():
    from pclink.models import AgentReport, ForegroundInfo

    agent = _bare_agent()
    report = AgentReport(session_id=3, ts=1.0, foreground=ForegroundInfo(pid=7, name="chrome.exe", title="t"),
                         url="https://mail.example.co.uk/x", domain="example.co.uk")
    message = agent._report_message(report, "mail.example.co.uk")
    assert message["type"] == "report" and message["domain"] == "example.co.uk" and message["host"] == "mail.example.co.uk"
    assert message["agent_pid"] == os.getpid() and message["foreground"]["created"] == 1700000000.5
    back = AgentReport.from_dict(message)                                    # the service still reads it
    assert back.domain == "example.co.uk" and back.foreground.pid == 7
    assert agent._report_message(AgentReport(session_id=3, ts=1.0), None)["foreground"] is None


def test_watchdog_decides_when_the_poll_hangs():
    from pclink.agent.main import Watchdog

    w = Watchdog(limit=60, period=5)
    assert not w.expired(0.0)                                                # nothing sampled yet: the clock starts
    w.beat(3.0)
    assert not w.expired(5.0) and not w.expired(10.0)
    for t in range(15, 65, 5):
        assert not w.expired(float(t))
    assert w.expired(65.0)                                                   # 62 s without a completed sample
    w.beat(66.0)
    assert not w.expired(70.0)
    # the PC slept: the checker itself was away, so the poll is not blamed
    assert not w.expired(4000.0) and not w.expired(4005.0)
    assert not Watchdog(limit=0).expired(10 ** 9)                            # switched off


def test_launched_programs_do_not_get_the_agent_token():
    from pclink.agent.main import child_environment

    env = {"PATH": "C:\\Windows", "PCLINK_AGENT_TOKEN": "secret", "PCLINK_IPC_PORT": "47391", "pclink_session": "2",
           "USERNAME": "kid"}
    assert child_environment(env) == {"PATH": "C:\\Windows", "USERNAME": "kid"}


def test_gsmtc_read_has_a_deadline(monkeypatch):
    import asyncio

    from pclink.agent import media

    monkeypatch.setattr(media, "GSMTC_TIMEOUT_SECONDS", 0.05)
    reader = media.MediaInfoReader(False)

    async def never():
        await asyncio.sleep(30)

    reader._gsmtc_async = never
    t0 = time.monotonic()
    assert reader._read_gsmtc() is None and time.monotonic() - t0 < 5        # gave up instead of hanging
    assert reader._gsmtc_failures == 1 and reader._gsmtc_retry_at > time.monotonic()
    loop = reader._loop
    deadline = time.monotonic() + 5
    while not reader._pending.done() and time.monotonic() < deadline:   # the late read was cancelled
        time.sleep(0.01)

    async def fine():
        return media.MediaInfo(title="Song", status="playing", source="gsmtc")

    reader._gsmtc_async, reader._gsmtc_retry_at = fine, 0.0
    assert reader._read_gsmtc().title == "Song" and reader._loop is loop     # the same event loop is used again
    loop.call_soon_threadsafe(loop.stop)
