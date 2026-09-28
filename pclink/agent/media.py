"""Now-playing information for the session.

Preferred source: the Windows *Global System Media Transport Controls*
(the same data the volume fly-out shows) through the optional ``winsdk`` /
``winrt`` packages.  Fallback: parse the foreground window title of known
players and browsers ("Artist - Song", "... - YouTube").
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

from ..models import ForegroundInfo, MediaInfo

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

_BROWSER_SUFFIXES = [
    " - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge", " — Mozilla Firefox", " - Mozilla Firefox",
    " - Brave", " - Opera", " - Vivaldi", " - Chromium", " – Mozilla Firefox",
]
_SITE_SUFFIXES = {
    " - YouTube": "YouTube", " - YouTube Music": "YouTube Music", " | Netflix": "Netflix", " - Netflix": "Netflix",
    " - Twitch": "Twitch", " | Prime Video": "Prime Video", " - Disney+": "Disney+", " | Spotify": "Spotify",
    " - SoundCloud": "SoundCloud", " - Deezer": "Deezer", " - Plex": "Plex",
}
_PLAYER_SUFFIXES = {
    "vlc.exe": " - VLC media player", "mpc-hc64.exe": " - MPC-HC", "mpc-hc.exe": " - MPC-HC", "wmplayer.exe": " - Windows Media Player",
    "potplayermini64.exe": " - PotPlayer", "potplayer.exe": " - PotPlayer", "mpv.exe": " - mpv", "foobar2000.exe": " [foobar2000]",
    "musicbee.exe": " - MusicBee", "itunes.exe": " - iTunes",
}
_ARTIST_TITLE_RE = re.compile(r"^(?P<artist>[^\-–—|]{1,80}?)\s+[\-–—]\s+(?P<title>.{1,120})$")


def parse_media_title(app: str, title: str) -> Optional[MediaInfo]:
    """Best-effort now-playing from a window title (pure; unit-tested)."""
    if not title:
        return None
    app = (app or "").lower()
    t = title.strip()
    if app == "spotify.exe":
        if t.lower() in ("spotify", "spotify premium", "spotify free", "advertisement", ""):
            return MediaInfo(app=app, status="paused", source="title")
        m = _ARTIST_TITLE_RE.match(t)
        if m:
            return MediaInfo(title=m.group("title").strip(), artist=m.group("artist").strip(), app=app, status="playing", source="title")
        return MediaInfo(title=t, app=app, status="playing", source="title")
    for suffix in _BROWSER_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)].rstrip()
            break
    for suffix, site in _SITE_SUFFIXES.items():
        if t.endswith(suffix):
            name = t[: -len(suffix)].strip()
            name = re.sub(r"^\(\d+\)\s*", "", name)  # "(3) Video title" notification counters
            playing = name.startswith("▶")
            name = name.lstrip("▶ ").strip()
            if not name or name.lower() == site.lower():
                return None
            return MediaInfo(title=name, artist="", app=site, status="playing" if playing else "unknown", source="title")
    for exe, suffix in _PLAYER_SUFFIXES.items():
        if app == exe:
            name = t[: -len(suffix)].strip() if t.endswith(suffix) else t
            if not name or name.lower() in ("vlc media player", "mpc-hc", "windows media player", "potplayer", "mpv", "foobar2000", "musicbee", "itunes"):
                return MediaInfo(app=app, status="stopped", source="title")
            m = _ARTIST_TITLE_RE.match(name)
            if m and exe in ("foobar2000.exe", "musicbee.exe", "itunes.exe"):
                return MediaInfo(title=m.group("title").strip(), artist=m.group("artist").strip(), app=app, status="unknown", source="title")
            return MediaInfo(title=name, app=app, status="unknown", source="title")
    return None


class MediaInfoReader:
    def __init__(self, enabled: bool = True, media_apps: Optional[list[str]] = None, browsers: Optional[list[str]] = None):
        self.enabled = enabled
        self.media_apps = {m.lower() for m in (media_apps or [])}
        self.browsers = {b.lower() for b in (browsers or [])}
        self._gsmtc = None
        self._gsmtc_failed = False
        self._last: Optional[MediaInfo] = None
        self._last_at = 0.0
        if enabled and _IS_WIN:
            self._init_gsmtc()

    def _init_gsmtc(self) -> None:
        for mod in ("winsdk.windows.media.control", "winrt.windows.media.control"):
            try:
                module = __import__(mod, fromlist=["GlobalSystemMediaTransportControlsSessionManager"])
                self._gsmtc = module
                log.info("now-playing via %s", mod)
                return
            except Exception:  # noqa: BLE001
                continue
        log.info("winsdk/winrt not installed; now-playing falls back to window titles")

    # -- public ----------------------------------------------------------------------------
    def read(self, fg: Optional[ForegroundInfo], audio_active: bool = False) -> Optional[MediaInfo]:
        if not self.enabled:
            return None
        info = self._read_gsmtc() if self._gsmtc is not None else None
        if info is None and fg is not None and fg.name:
            name = fg.name.lower()
            if name in self.media_apps or name in self.browsers or name == "spotify.exe":
                info = parse_media_title(name, fg.title)
                if info is not None and info.status == "unknown":
                    info.status = "playing" if audio_active else "paused"
        self._last = info
        return info

    # -- GSMTC -------------------------------------------------------------------------------
    def _read_gsmtc(self) -> Optional[MediaInfo]:
        if self._gsmtc_failed:
            return None
        try:
            import asyncio

            return asyncio.run(self._gsmtc_async())
        except Exception as exc:  # noqa: BLE001
            log.debug("GSMTC read failed: %s", exc)
            self._gsmtc_failed = True
            return None

    async def _gsmtc_async(self) -> Optional[MediaInfo]:
        Manager = self._gsmtc.GlobalSystemMediaTransportControlsSessionManager
        manager = await Manager.request_async()
        session = manager.get_current_session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        status_code = int(getattr(info.playback_status, "value", info.playback_status))
        status = {0: "stopped", 1: "stopped", 2: "playing", 3: "stopped", 4: "playing", 5: "paused"}.get(status_code, "unknown")
        app = str(session.source_app_user_model_id or "")
        return MediaInfo(title=str(props.title or ""), artist=str(props.artist or ""), app=app, status=status, source="gsmtc")
