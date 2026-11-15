import json
import sys
import types
from pathlib import Path

import httpx
import pytest

from src.__main__ import (
    MAX_HTML_BYTES,
    AdsCrawlAPIError,
    InputError,
    extract,
    ordinary_fetch,
    parse_input,
    rendered_fetch,
    validate_public_url,
)


def test_input_validation_and_hostname_safety():
    with pytest.raises(InputError):
        parse_input({"urls": [{"url": "https://example.com"}]})
    for url in [
        "http://localhost/",
        "http://10.0.0.1/",
        "http://2130706433/",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://singlelabel/",
        "https://u:p@example.com/",
        "https://example.com\\evil/",
        "https://example.com/\nnext",
    ]:
        with pytest.raises(InputError):
            validate_public_url(url)
    with pytest.raises(InputError, match="Invalid hostname"):
        validate_public_url("https://bad..example/")
    with pytest.raises(InputError, match="Invalid CSS selector"):
        parse_input({"urls": [{"url": "https://example.com", "render": False, "selector": "["}]})


def test_ordinary_only_input_does_not_need_api_key():
    jobs, timeout = parse_input({"urls": [{"url": "https://example.com", "render": False}]})
    assert jobs[0].render is False
    assert timeout == 20.0


@pytest.mark.asyncio
async def test_ordinary_fetch_streams_and_validates_redirect_target():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private", "set-cookie": "secret=1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InputError):
            await ordinary_fetch(client, "https://example.com/")
    assert len(client.cookies) == 0


@pytest.mark.asyncio
async def test_ordinary_fetch_does_not_forward_set_cookie_between_inputs():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "session=secret"}, content=b"<p>ok</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ordinary_fetch(client, "https://example.com/one")
        await ordinary_fetch(client, "https://example.com/two")
    assert seen == ["", ""]


@pytest.mark.asyncio
async def test_rendered_fetch_uses_raw_html_contract_and_local_selector_only():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><title>Rendered</title></html>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        url, body, status = await rendered_fetch(client, "https://example.com/app", "test-only-key", ".card")
    assert url == "https://example.com/app"
    assert status == 200
    assert body.startswith(b"<html>")
    assert seen["json"] == {"url": "https://example.com/app", "contentMode": "html"}
    assert seen["headers"]["x-api-key"] == "test-only-key"


@pytest.mark.asyncio
async def test_rendered_fetch_parses_sanitized_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            headers={"content-type": "application/json"},
            json={"error": "secret detail", "code": "INSUFFICIENT_CREDITS", "key": "do-not-copy"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdsCrawlAPIError) as caught:
            await rendered_fetch(client, "https://example.com/", "test-only-key")
    assert caught.value.status == 402
    assert caught.value.code == "INSUFFICIENT_CREDITS"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_rendered_json_success_is_rejected_by_html_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"html": "<p>x</p>"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InputError, match="unexpected response format"):
            await rendered_fetch(client, "https://example.com/", "test-only-key")


@pytest.mark.asyncio
async def test_response_size_is_capped_while_streaming():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * (MAX_HTML_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InputError):
            await ordinary_fetch(client, "https://example.com/")


def test_extract_returns_all_local_selector_matches():
    result = extract(
        "https://example.com",
        "https://example.com",
        b"<p class='x'>one</p><p class='x'>two</p>",
        ".x",
        False,
        200,
    )
    assert [item["text"] for item in result["items"]] == ["one", "two"]
    assert result["finalUrl"] == "https://example.com"
    assert result["status"] == 200
    assert result["apiStatus"] is None


def test_extract_rendered_uses_api_status_without_target_metadata():
    result = extract(
        "https://example.com/app",
        "https://example.com/redirected",
        b"<title>Rendered</title><p>content</p>",
        None,
        True,
        200,
    )
    assert result["finalUrl"] is None
    assert result["status"] is None
    assert result["apiStatus"] == 200


@pytest.mark.asyncio
async def test_run_pushes_invalid_input_and_signals_failure(monkeypatch):
    calls = []

    class FakeActor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get_input(self):
            return {"urls": [{"url": "https://example.com", "render": False, "selector": "["}]}

        async def push_data(self, value):
            calls.append(("push", value))

        async def fail(self, message):
            calls.append(("fail", message))

    monkeypatch.setitem(sys.modules, "apify", types.SimpleNamespace(Actor=FakeActor()))
    from src.__main__ import run

    await run()
    assert calls[0] == ("push", {"error": "invalid_input", "message": "Invalid CSS selector"})
    assert calls[1] == ("fail", "Invalid actor input")


def test_actor_metadata_paths_and_schema_secret():
    root = Path(__file__).parents[1]
    metadata = json.loads((root / ".actor" / "actor.json").read_text())
    assert metadata["dockerfile"] == "../Dockerfile"
    assert metadata["dockerContextDir"] == ".."
    assert metadata["readme"] == "../README.md"
    assert metadata["input"] == "./input_schema.json"
    schema = json.loads((root / ".actor" / "input_schema.json").read_text())
    assert schema["properties"]["urls"]["editor"] == "schemaBased"
    assert schema["properties"]["adscrawlApiKey"]["isSecret"] is True
