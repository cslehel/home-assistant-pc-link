"""Best-effort active-tab URL from a browser's address bar via UI Automation.

Reading the omnibox with UI Automation is read-only and does not inject into
the browser, which keeps antivirus and the browsers themselves happy.  It is
throttled hard: the (relatively expensive) UIA walk only runs when the
foreground window handle or title actually changes, and it is fully optional.
When ``comtypes`` / UIAutomation is missing, the domain is derived from the
window title instead, and failing that it is simply omitted.
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

from ..util import domain_from_url

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"
_TITLE_DOMAIN_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)

# UIAutomation control type ids
_UIA_EDIT = 50004
_UIA_VALUE_PROPERTY = 30045
_UIA_CONTROLTYPE_PROPERTY = 30003


class BrowserUrlReader:
    def __init__(self, method: str = "uia", group_by: str = "registrable"):
        self.method = method if _IS_WIN else "title"
        self.group_by = group_by
        self._uia = None
        self._cache_key = None
        self._cache_value: Optional[str] = None
        if self.method == "uia":
            try:
                self._init_uia()
            except Exception as exc:  # noqa: BLE001
                log.info("UI Automation unavailable, falling back to window titles: %s", exc)
                self.method = "title"

    def _init_uia(self):
        import comtypes.client  # type: ignore

        # Generate the UI Automation wrapper on first use (comtypes caches it; in the
        # standalone build the cache lives in the user's AppData).  Importing
        # comtypes.gen.UIAutomationClient directly only works if something else
        # happened to generate it before.
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore

        self._comtypes = __import__("comtypes")
        self._UIA = UIA
        self._uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

    # -- public --------------------------------------------------------------
    def url_and_domain(self, hwnd: int, title: str) -> tuple[Optional[str], Optional[str]]:
        key = (hwnd, title)
        if key == self._cache_key:
            url = self._cache_value
            return url, domain_from_url(url or title, self.group_by)
        self._cache_key = key
        url = None
        if self.method == "uia" and hwnd:
            try:
                url = self._read_omnibox(hwnd)
            except Exception as exc:  # noqa: BLE001
                log.debug("omnibox read failed: %s", exc)
        self._cache_value = url
        domain = domain_from_url(url, self.group_by) if url else self._domain_from_title(title)
        return url, domain

    def domain_only(self, hwnd: int, title: str) -> Optional[str]:
        return self.url_and_domain(hwnd, title)[1]

    # -- internals -----------------------------------------------------------
    def _domain_from_title(self, title: str) -> Optional[str]:
        if not title:
            return None
        m = _TITLE_DOMAIN_RE.search(title)
        if m:
            return domain_from_url(m.group(1), self.group_by)
        return None

    def _read_omnibox(self, hwnd: int) -> Optional[str]:
        uia = self._uia
        try:
            element = uia.ElementFromHandle(hwnd)
            if not element:
                return None
            cond = uia.CreatePropertyCondition(_UIA_CONTROLTYPE_PROPERTY, _UIA_EDIT)
            TreeScope_Descendants = 4
            edits = element.FindAll(TreeScope_Descendants, cond)
            if not edits:
                return None
            for i in range(min(edits.Length, 4)):
                edit = edits.GetElement(i)
                try:
                    value = edit.GetCurrentPropertyValue(_UIA_VALUE_PROPERTY)
                except Exception:  # noqa: BLE001
                    continue
                if value and isinstance(value, str) and ("." in value or "/" in value):
                    return value.strip()
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("UIA read failed: %s", exc)
            return None
