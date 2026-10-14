"""Small, dependency-free helpers shared by the service and the agent."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from datetime import datetime, timedelta, tzinfo
from typing import Callable, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlsplit

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# A limit override stored as this (any negative number is read the same way) means
# "no limit", as opposed to no override at all - see TimeKeeper.set_limit_today and
# RuleEngine.set_rule_limit_today.
NO_LIMIT = -1


def slugify(text: object, sep: str = "_") -> str:
    """Lower-case ASCII identifier that is safe for HA object ids.

    Accented letters are transliterated ("Núñez" -> "nunez").  A name that has no
    ASCII form at all ("Петя", "太郎") gets a stable id of its own - "u" and eight
    hex digits of the SHA-1 of the lower-cased name - instead of the "x" every such
    name used to share."""
    raw = str(text)
    normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub(sep, normalized.lower()).strip(sep)
    if slug:
        return slug
    if any(ch.isalnum() for ch in raw):  # only non-ASCII letters / digits can be left here
        return "u" + hashlib.sha1(raw.strip().lower().encode("utf-8")).hexdigest()[:8]
    return "x"


# ---------------------------------------------------------------------------
# Domains / URLs
# ---------------------------------------------------------------------------

# A pragmatic subset of the public suffix list: two-label suffixes under which
# the "registrable" domain is three labels long (e.g. bbc.co.uk).
_TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "org.nz", "net.nz", "govt.nz",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
        "com.br", "net.br", "org.br", "gov.br",
        "com.mx", "com.ar", "com.co", "com.pe", "com.ve", "com.uy", "com.ec",
        "co.za", "org.za", "co.in", "net.in", "org.in", "gov.in",
        "com.tr", "com.cn", "net.cn", "org.cn", "com.hk", "com.sg", "com.tw",
        "co.kr", "or.kr", "com.ua", "com.pl", "co.il", "org.il", "com.eg",
        "com.sa", "com.my", "co.th", "com.vn", "com.ph", "co.id", "com.pk",
        "com.ng", "co.ke", "com.ru", "org.ru", "net.ru", "com.hu", "co.hu",
        "org.hu", "com.ro", "co.at", "or.at", "co.it", "co.nl", "com.es",
        "com.pt", "com.gr", "co.cz", "com.se", "co.no", "co.dk",
    }
)

# Country domains outside the list above that register names under a generic second
# level (example.co.rs, example.com.xx, example.ac.xx ...): the same three-label rule.
_GENERIC_SECOND_LEVELS = frozenset(
    {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go", "govt", "mil", "sch", "nom"}
)

# Well-known hosting suffixes under which every customer has a site of their own
# (kevin.github.io and somebody-else.github.io are different websites).
_PRIVATE_SUFFIXES = frozenset(
    {
        "github.io", "gitlab.io", "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
        "netlify.app", "vercel.app", "herokuapp.com", "appspot.com", "azurewebsites.net",
        "cloudfront.net", "glitch.me", "repl.co", "blogspot.com", "wordpress.com",
        "wixsite.com", "weebly.com", "neocities.org",
    }
)

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.I)
_HOST_PORT_RE = re.compile(r"^[a-z0-9.\-]+:\d{1,5}(/|$)", re.I)
_ALLOWED_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss"})


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def registrable_domain(host: str) -> str:
    """Collapse a hostname to its registrable domain (www.youtube.com -> youtube.com)."""
    host = host.lower().strip().rstrip(".")
    if not host or is_ip(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in _TWO_LEVEL_SUFFIXES or suffix in _PRIVATE_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVELS:
        return ".".join(labels[-3:])
    return suffix


def _ascii_host(host: str) -> Optional[str]:
    """``host`` in its ASCII (punycode) form: "münchen.de" -> "xn--mnchen-3ya.de".  None when
    it is not a valid host name.  The address bar shows such names in Unicode; rules and
    statistics use the one form that is the same everywhere."""
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None


def domain_from_url(text: Optional[str], group: str = "registrable") -> Optional[str]:
    """Extract a website identifier from an address-bar string.

    Browsers hide the scheme (and often "www.") in the omnibox, and the omnibox
    may contain typed search text, so this is deliberately defensive: anything
    that does not look like a hostname returns ``None``.
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if "://" not in text:
        if _SCHEME_RE.match(text) and not _HOST_PORT_RE.match(text):
            # about:blank, chrome:flags, edge://settings, mailto:, javascript:, ...
            return None
        text = "http://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    host = _ascii_host(host.lower().rstrip("."))
    if not host or not (is_ip(host) or _HOST_RE.match(host)):
        return None
    if "." not in host and host != "localhost" and not is_ip(host):
        # A single word without a dot is almost always typed search text.
        return None
    if group == "host":
        return host
    return registrable_domain(host)


def normalize_site(raw: object) -> Optional[str]:
    """A website as somebody wrote it -> the host name rules are matched against.

    ``youtube.com``, ``*.youtube.com``, ``www.youtube.com`` and
    ``https://youtube.com/watch?v=...`` are all ``youtube.com``; ``docs.google.com``
    stays what it is; an IDN is given in punycode and an IP address as the address.
    None when the text is not a website at all.  (The same result as items blocked
    from Home Assistant get.)"""
    text = str(raw or "").strip().lower().lstrip("*.")
    if not text or len(text) > 2000:
        return None
    host = domain_from_url(text, "host")
    if not host or ("." not in host and not is_ip(host)):
        return None
    return host[4:] if host.startswith("www.") and host.count(".") >= 2 else host


# ---------------------------------------------------------------------------
# Time-of-day windows ("07:00-21:00")
# ---------------------------------------------------------------------------

Window = Tuple[int, int]  # start minute (inclusive), end minute (exclusive), 0..1440

_HHMM_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


def parse_hhmm(value: object) -> int:
    """'07:30' -> 450 minutes since midnight. '24:00' -> 1440."""
    s = str(value).strip()
    m = _HHMM_RE.match(s)
    if not m:
        raise ValueError(f"invalid time of day: {value!r} (expected HH:MM)")
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    if hours == 24 and minutes == 0:
        return 1440
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"invalid time of day: {value!r}")
    return hours * 60 + minutes


def parse_windows(spec: object) -> list[Window]:
    """Parse an allowed-hours spec into sorted windows.

    * ``None`` / ``"all"`` -> ``[]`` (no restriction)
    * ``"never"`` -> ``[(0, 0)]`` (never allowed)
    * ``"07:00-21:00"`` -> ``[(420, 1260)]``
    * ``"22:00-02:00"`` -> ``[(0, 120), (1320, 1440)]`` (wraps midnight)
    * ``"07:00-09:00, 16:00-20:00"`` or a list of strings -> several windows
    """
    day, spill = parse_windows_split(spec)
    return sorted(set(day + spill))


def parse_windows_split(spec: object) -> tuple[list[Window], list[Window]]:
    """Like :func:`parse_windows`, but keeps the part of a wrapped range apart.

    Returns ``(windows, spill)``: ``windows`` belong to the day the spec is
    written for, ``spill`` is the after-midnight part of ranges such as
    ``"16:00-01:00"`` and belongs to the *next* day (``[(0, 60)]`` here).
    ``parse_windows`` folds both into the same day, which is only right when
    every day has the same spec.

    A range whose start equals its end is rejected (it used to mean "the whole
    day", which turned a typo into no restriction at all); only
    ``"00:00-00:00"`` is still read as the whole day.  ``24:00`` can end a range
    but not start one.
    """
    if spec is None:
        return [], []
    if isinstance(spec, (list, tuple)):
        day_out: list[Window] = []
        spill_out: list[Window] = []
        for item in spec:
            day, spill = parse_windows_split(item)
            day_out.extend(day)
            spill_out.extend(spill)
        return sorted(set(day_out)), sorted(set(spill_out))
    s = str(spec).strip().lower()
    if not s or s in {"all", "always", "any", "*", "unlimited"}:
        return [], []
    if s in {"none", "never"}:
        return [(0, 0)], []
    windows: list[Window] = []
    spill_windows: list[Window] = []
    for part in re.split(r"[,;]", s):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"invalid time window {part!r} (expected HH:MM-HH:MM)")
        start_s, end_s = part.split("-", 1)
        start, end = parse_hhmm(start_s), parse_hhmm(end_s)
        if start == 1440:
            # 24:00 is the end of a day: as a start ("24:00-07:00") it read as an empty window
            # plus the morning, which blocked the rest of the day - write 00:00 instead
            raise ValueError(f"invalid time window {part!r} (24:00 can only end a window; "
                             f"start it at 00:00)")
        if end > start:
            windows.append((start, end))
        elif end < start:
            windows.append((start, 1440))
            if end > 0:
                spill_windows.append((0, end))
        elif start == 0:  # "00:00-00:00": whole day
            windows.append((0, 1440))
        else:
            raise ValueError(f"invalid time window {part!r} (start and end are the same)")
    return sorted(set(windows)), sorted(set(spill_windows))


def window_status(
    minute_of_day: float,
    windows: Sequence[Window],
    next_day_windows: Optional[Sequence[Window]] = None,
) -> tuple[bool, Optional[float]]:
    """Return ``(inside, minutes_until_window_end)``.

    ``windows`` empty means no restriction -> ``(True, None)``.  When the
    current window runs to midnight and tomorrow's schedule starts at midnight,
    the remaining time is extended into tomorrow so users are not warned about
    a fake cut-off at 23:59.
    """
    if not windows:
        return True, None
    for start, end in windows:
        if start <= minute_of_day < end:
            edge = end
            changed = True
            while changed:  # merge contiguous / overlapping windows
                changed = False
                for s2, e2 in windows:
                    if s2 <= edge < e2:
                        edge = e2
                        changed = True
            remaining = edge - minute_of_day
            if edge >= 1440 and next_day_windows:
                for s2, e2 in next_day_windows:
                    if s2 == 0:
                        remaining += e2
                        break
            elif edge >= 1440 and next_day_windows is not None and not next_day_windows:
                remaining += 1440  # tomorrow unrestricted
            return True, remaining
    return False, None


def windows_cover(outer: Sequence[Window], inner: Sequence[Window]) -> bool:
    """Is every allowed minute of ``inner`` also allowed by ``outer``?  (``[]`` = the whole day.)"""
    if not outer:
        return True
    for start, end in (inner or [(0, 1440)]):
        at = start
        for s, e in sorted(outer):
            if s <= at < e:
                at = e
        if at < end:
            return False
    return True


# Windows time zone key name -> IANA name, for the zones people commonly have (the
# CLDR "windowsZones" mapping, the default territory of each).
_WINDOWS_ZONES = {
    "UTC": "Etc/UTC", "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kyiv",
    "GTB Standard Time": "Europe/Bucharest", "Turkey Standard Time": "Europe/Istanbul",
    "Russian Standard Time": "Europe/Moscow", "Belarus Standard Time": "Europe/Minsk",
    "Kaliningrad Standard Time": "Europe/Kaliningrad", "Israel Standard Time": "Asia/Jerusalem",
    "Egypt Standard Time": "Africa/Cairo", "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos", "E. Africa Standard Time": "Africa/Nairobi",
    "Morocco Standard Time": "Africa/Casablanca", "Arab Standard Time": "Asia/Riyadh",
    "Arabian Standard Time": "Asia/Dubai", "Arabic Standard Time": "Asia/Baghdad", "Iran Standard Time": "Asia/Tehran",
    "Pakistan Standard Time": "Asia/Karachi", "India Standard Time": "Asia/Kolkata",
    "Bangladesh Standard Time": "Asia/Dhaka", "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai", "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei", "Tokyo Standard Time": "Asia/Tokyo", "Korea Standard Time": "Asia/Seoul",
    "W. Australia Standard Time": "Australia/Perth", "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin", "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney", "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage", "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Standard Time (Mexico)": "America/Tijuana", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City", "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time": "America/New_York", "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Atlantic Standard Time": "America/Halifax", "Newfoundland Standard Time": "America/St_Johns",
    "SA Pacific Standard Time": "America/Bogota", "Venezuela Standard Time": "America/Caracas",
    "SA Western Standard Time": "America/La_Paz", "Pacific SA Standard Time": "America/Santiago",
    "E. South America Standard Time": "America/Sao_Paulo", "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "SA Eastern Standard Time": "America/Cayenne", "Central America Standard Time": "America/Guatemala",
}
_WINDOWS_ZONES_LOWER = {key.lower(): value for key, value in _WINDOWS_ZONES.items()}


def windows_zone_to_iana(name: object) -> Optional[str]:
    """The IANA name for a Windows time zone ("Central Europe Standard Time" ->
    "Europe/Budapest"); None for a zone that is not in the table.  Windows names a zone
    by its standard time all year, and ``time.tzname`` gives the daylight name in summer:
    "... Daylight Time" / "... Summer Time" are read as the same zone."""
    text = " ".join(str(name or "").split()).lower()
    if not text:
        return None
    for ending in (" daylight time", " summer time"):
        if text.endswith(ending):
            text = text[: -len(ending)] + " standard time"
    return _WINDOWS_ZONES_LOWER.get(text)


def _windows_zone_key() -> Optional[str]:
    """The key name of the PC's time zone from the registry (Windows only)."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            value, _kind = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    return str(value).split("\x00", 1)[0].strip() or None


def local_iana_zone() -> Optional[str]:
    """The PC's current time zone as an IANA name, None when it cannot be told.

    On Windows the zone's key name comes from the registry (it does not depend on the
    display language), else from ``time.tzname``; elsewhere the ``TZ`` variable is
    taken when it holds an IANA name.  Only a name the zone database knows is returned."""
    import os
    import time as _time

    candidates = [windows_zone_to_iana(_windows_zone_key())]
    candidates += [windows_zone_to_iana(name) for name in _time.tzname]
    env = os.environ.get("TZ", "").lstrip(":")
    if "/" in env:
        candidates.append(env)
    for zone in candidates:
        if not zone:
            continue
        try:
            time_zone(zone)
        except ValueError:
            continue
        return zone
    return None


def time_zone(name: str) -> tzinfo:
    """The time zone called ``name`` ("Europe/Budapest"); ``ValueError`` when it is not known.

    Windows has no time zone database of its own for Python: there the ``tzdata``
    package provides it (it is part of the requirements and of the standalone build)."""
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(str(name))
    except Exception as exc:  # noqa: BLE001 - not found, a malformed key, a broken database
        raise ValueError(f"unknown time zone {name!r} (use a name such as Europe/Budapest; "
                         f"a Python install needs the tzdata package): {exc}") from None


_EPOCH = datetime(1970, 1, 1)


class LocalTimezone(tzinfo):
    """The PC's local time as a ``tzinfo``, asked of the operating system each time
    (``time.localtime``), with its daylight-saving changes - for when no IANA zone can be
    told.  A local "now" that carries it knows its real UTC offset, so "how many real
    seconds until 04:00" is right on the nights the clocks change, as with a named zone.

    Wall times in the hour that is repeated in autumn and in the hour skipped in spring are
    resolved as PEP 495 says: ``fold=0`` takes the offset from before the change, ``fold=1``
    the one after."""

    @staticmethod
    def _offset_at(ts: float) -> int:
        """The UTC offset in seconds at the moment ``ts`` (0 outside what the OS can convert)."""
        import time as _time

        try:
            return int(_time.localtime(ts).tm_gmtoff or 0)
        except (OSError, OverflowError, ValueError):
            try:
                return int(_time.localtime().tm_gmtoff or 0)
            except Exception:  # noqa: BLE001
                return -_time.timezone

    def _offset_of_wall(self, dt: datetime) -> int:
        wall = (dt.replace(tzinfo=None, fold=0) - _EPOCH).total_seconds()
        before, after = self._offset_at(wall - 86400), self._offset_at(wall + 86400)
        if before == after:
            return before
        ok_before = self._offset_at(wall - before) == before
        ok_after = self._offset_at(wall - after) == after
        if ok_before != ok_after:      # only one reading exists: an ordinary time
            return before if ok_before else after
        # both (the repeated hour) or neither (the skipped hour)
        return after if getattr(dt, "fold", 0) else before

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return timedelta(seconds=self._offset_at(_now_ts()))
        return timedelta(seconds=self._offset_of_wall(dt))

    def dst(self, dt: Optional[datetime]) -> timedelta:
        import time as _time

        ts = _now_ts() if dt is None else (dt.replace(tzinfo=None) - _EPOCH).total_seconds() - self._offset_of_wall(dt)
        try:
            isdst = _time.localtime(ts).tm_isdst > 0
        except (OSError, OverflowError, ValueError):
            isdst = False
        return (self.utcoffset(dt) - timedelta(seconds=-_time.timezone)) if isdst else timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> Optional[str]:
        import time as _time

        ts = _now_ts() if dt is None else (dt.replace(tzinfo=None) - _EPOCH).total_seconds() - self._offset_of_wall(dt)
        try:
            return _time.localtime(ts).tm_zone
        except (OSError, OverflowError, ValueError):
            return None

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        ts = (dt.replace(tzinfo=None) - _EPOCH).total_seconds()
        offset = self._offset_at(ts)
        local = dt.replace(tzinfo=None) + timedelta(seconds=offset)
        out = local.replace(tzinfo=self, fold=0)
        if self._offset_of_wall(out) != offset:
            out = out.replace(fold=1)     # the second time this wall time comes round
        return out

    def __repr__(self) -> str:
        return "LocalTimezone()"


def _now_ts() -> float:
    import time as _time

    return _time.time()


LOCAL_TZ = LocalTimezone()


def make_clock(zone: Optional[str] = None) -> Callable[[], datetime]:
    """The service's clock.  Without ``zone`` it is the PC's local time - zone-aware all the
    same (:class:`LocalTimezone`), so real seconds are right across a change to or from
    daylight-saving time; with one, "now" in that zone whatever Windows is set to, so
    changing the PC's time zone moves no day boundary."""
    if not zone:
        return lambda: datetime.now(LOCAL_TZ)
    tz = time_zone(zone)
    return lambda: datetime.now(tz)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def human_minutes(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unlimited"
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def first(iterable: Iterable, default=None):
    for item in iterable:
        return item
    return default
