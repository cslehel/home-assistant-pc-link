"""The REST client itself (the other tests replace it with a fake): paths and error mapping."""
from __future__ import annotations

from typing import Any, Optional

import pytest

from custom_components.pclink.api import (
    PclinkApiError,
    PclinkAuthError,
    PclinkClient,
    PclinkOutdatedError,
    user_path,
)


class _Response:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status, self._body = status, body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode()

    async def json(self, content_type: Optional[str] = None) -> Any:
        import json

        return json.loads(self._body)


class _Session:
    """Just enough of aiohttp.ClientSession: answers by path, records what was asked."""

    def __init__(self, answers: dict[str, _Response]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, url: str, headers: Optional[dict[str, str]] = None, json: Any = None) -> _Response:
        self.calls.append((method, url, dict(headers or {})))
        path = url.split("47392", 1)[1]
        return self.answers.get(path, _Response(404, b'{"error": "not found"}'))


def _client(answers: dict[str, _Response]) -> tuple[PclinkClient, _Session]:
    session = _Session(answers)
    return PclinkClient(session, "192.168.1.50", 47392, "secret"), session  # type: ignore[arg-type]


def test_user_names_are_percent_encoded() -> None:
    assert user_path("kevin", "/screenshot") == "/api/users/kevin/screenshot"
    assert user_path("john doe", "/screenshot") == "/api/users/john%20doe/screenshot"
    assert user_path("a/b#c?d%e") == "/api/users/a%2Fb%23c%3Fd%25e"
    assert user_path("zoë") == "/api/users/zo%C3%AB"


async def test_screenshot_path_auth_header_and_missing_image() -> None:
    client, session = _client({"/api/users/john%20doe/screenshot": _Response(200, b"IMG")})
    assert await client.screenshot("john doe") == b"IMG"
    method, url, headers = session.calls[-1]
    assert (method, url) == ("GET", "http://192.168.1.50:47392/api/users/john%20doe/screenshot")
    assert headers == {"Authorization": "Bearer secret"} and "secret" not in url
    assert await client.screenshot("nobody") is None  # 404 = no screenshot yet


async def test_error_mapping() -> None:
    client, _ = _client({"/api/entities": _Response(500, b'{"error": "boom"}'),
                         "/api/users/kevin/screenshot": _Response(401, b"{}"),
                         "/api/config": _Response(401, b"{}")})
    with pytest.raises(PclinkApiError) as err:  # a failing PC is not "too old"
        await client.entities()
    assert not isinstance(err.value, PclinkOutdatedError)
    with pytest.raises(PclinkAuthError):
        await client.screenshot("kevin")
    with pytest.raises(PclinkAuthError):
        await client.check_write_auth()
    old, _ = _client({})  # no /api/entities at all: an old service
    with pytest.raises(PclinkOutdatedError):
        await old.entities()
    await old.check_write_auth()  # and no /api/config says nothing about the token
