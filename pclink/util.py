"""Small, dependency-free helpers shared by the service and the agent."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Iterable, Optional, Sequence, Tuple
from urllib.parse import urlsplit

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: object, sep: str = "_") -> str:
    """Lower-case ASCII identifier that is safe for MQTT topics and HA object ids."""
    normalized = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub(sep, normalized.lower()).strip(sep)
    return slug or "x"


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
    if ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


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
    host = host.lower().rstrip(".")
    if not (is_ip(host) or _HOST_RE.match(host)):
        return None
    if "." not in host and host != "localhost" and not is_ip(host):
        # A single word without a dot is almost always typed search text.
        return None
    if group == "host":
        return host
    return registrable_domain(host)


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
    if spec is None:
        return []
    if isinstance(spec, (list, tuple)):
        out: list[Window] = []
        for item in spec:
            out.extend(parse_windows(item))
        return sorted(set(out))
    s = str(spec).strip().lower()
    if not s or s in {"all", "always", "any", "*", "unlimited"}:
        return []
    if s in {"none", "never"}:
        return [(0, 0)]
    windows: list[Window] = []
    for part in re.split(r"[,;]", s):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"invalid time window {part!r} (expected HH:MM-HH:MM)")
        start_s, end_s = part.split("-", 1)
        start, end = parse_hhmm(start_s), parse_hhmm(end_s)
        if end > start:
            windows.append((start, end))
        elif end < start:
            windows.append((start, 1440))
            windows.append((0, end))
        else:  # "00:00-00:00" style: whole day
            windows.append((0, 1440))
    return sorted(set(windows))


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
