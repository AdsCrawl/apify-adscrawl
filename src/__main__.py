from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector

API_ENDPOINT = "https://api.adscrawl.net/html"
MAX_URLS = 25
MAX_HTML_BYTES = 2_000_000
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 60.0
ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,80}$")
log = logging.getLogger(__name__)


class InputError(ValueError):
    pass


class AdsCrawlAPIError(RuntimeError):
    def __init__(self, status: int, code: str | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__("AdsCrawl API request failed")


@dataclass(frozen=True)
class Job:
    url: str
    render: bool
    selector: str | None


def validate_public_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise InputError("Each URL must be a non-empty string of at most 4096 characters")
    url = value.strip()
    if "\\" in url or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise InputError("URLs must not contain control characters or backslashes")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise InputError("Invalid URL") from None
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise InputError("URLs must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise InputError("URLs must not contain embedded credentials")
    if port not in {None, 80, 443}:
        raise InputError("Only ports 80 and 443 are supported")
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise InputError("Local or internal hosts are not allowed")
    # This catches literal IPs, including decimal/hex/octal shorthand accepted by
    # some URL clients. DNS hostnames are not resolved here; DNS rebinding remains
    # outside this actor's validation boundary.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) == 1:
            raise InputError("Single-label hosts are not allowed")
        # Reject legacy integer, octal, hexadecimal, and shortened dotted IPv4
        # spellings such as 127.1 and 0x7f.0.0.1.
        if all(re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", label) for label in labels):
            raise InputError("Malformed or non-public IP addresses are not allowed")
        if any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or not re.fullmatch(r"[A-Za-z0-9-]+", label)
            for label in labels
        ):
            raise InputError("Invalid hostname")
        return url
    if not address.is_global:
        raise InputError("Non-public IP addresses are not allowed")
    return url


def validate_selector(selector: Any) -> str | None:
    if selector is None:
        return None
    if not isinstance(selector, str) or not selector.strip() or len(selector) > 500:
        raise InputError("selector must be a non-empty string of at most 500 characters")
    normalized = selector.strip()
    try:
        compile_selector(normalized)
    except (SelectorSyntaxError, ValueError):
        raise InputError("Invalid CSS selector") from None
    return normalized


def parse_input(raw: Any) -> tuple[list[Job], float]:
    if not isinstance(raw, dict):
        raise InputError("Input must be an object")
    entries = raw.get("urls")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_URLS:
        raise InputError(f"urls must contain 1 to {MAX_URLS} items")
    timeout = raw.get("timeoutSeconds", DEFAULT_TIMEOUT)
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
        raise InputError(f"timeoutSeconds must be between 1 and {MAX_TIMEOUT}")
    jobs: list[Job] = []
    for item in entries:
        if not isinstance(item, dict):
            raise InputError("Each urls item must be an object")
        url = validate_public_url(item.get("url"))
        render = item.get("render")
        if type(render) is not bool:
            raise InputError("Each urls item must explicitly set render to true or false")
        jobs.append(Job(url, render, validate_selector(item.get("selector"))))
    return jobs, float(timeout)


def safe_error(status: int | None = None, body: bytes = b"") -> dict[str, Any]:
    result: dict[str, Any] = {"error": "request_failed"}
    if status is not None:
        result["status"] = status
        try:
            parsed = json.loads(body)
            code = parsed.get("code") if isinstance(parsed, dict) else None
            if isinstance(code, str) and ERROR_CODE.fullmatch(code):
                result["code"] = code
        except (ValueError, UnicodeDecodeError):
            pass
    return result


async def read_limited(response: httpx.Response, limit: int = MAX_HTML_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise InputError("HTML response exceeded the configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _close_cookie_state(client: httpx.AsyncClient) -> None:
    client.cookies.clear()


async def ordinary_fetch(client: httpx.AsyncClient, url: str) -> tuple[str, bytes, int]:
    current = validate_public_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            async with client.stream("GET", current, follow_redirects=False, headers={"cookie": ""}) as response:
                status = response.status_code
                location = response.headers.get("location")
                if status in {301, 302, 303, 307, 308}:
                    if not location:
                        return current, b"", status
                    current = validate_public_url(urljoin(current, location))
                    continue
                body = await read_limited(response)
        finally:
            await _close_cookie_state(client)
        return current, body, status
    raise InputError("Too many redirects")


async def rendered_fetch(
    client: httpx.AsyncClient, url: str, api_key: str, selector: str | None = None
) -> tuple[str, bytes, int]:
    # selector is intentionally local-only: AdsCrawl selector extraction returns one
    # target, while this actor must expose all matches from the returned DOM.
    payload = {"url": validate_public_url(url), "contentMode": "html"}
    try:
        async with client.stream(
            "POST",
            API_ENDPOINT,
            json=payload,
            headers={
                "x-api-key": api_key,
                "accept": "text/html",
                "content-type": "application/json",
                "cookie": "",
            },
            follow_redirects=False,
        ) as response:
            body = await read_limited(response)
            status = response.status_code
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    finally:
        await _close_cookie_state(client)
    if status != 200:
        code = None
        try:
            parsed = json.loads(body)
            candidate = parsed.get("code") if isinstance(parsed, dict) else None
            if isinstance(candidate, str) and ERROR_CODE.fullmatch(candidate):
                code = candidate
        except (ValueError, UnicodeDecodeError):
            pass
        raise AdsCrawlAPIError(status, code)
    if content_type != "text/html":
        raise InputError("AdsCrawl returned an unexpected response format")
    return url, body, status


def extract(
    url: str,
    final_url: str | None,
    html: bytes,
    selector: str | None,
    rendered: bool,
    status: int | None,
    api_status: int | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select(selector) if selector else [soup]
    root = roots[0] if roots else None
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    text = " ".join(root.stripped_strings) if root else ""
    items = [{"text": " ".join(node.stripped_strings)} for node in roots] if selector else []
    return {
        "url": url,
        "finalUrl": None if rendered else final_url,
        "status": None if rendered else status,
        "apiStatus": (api_status if api_status is not None else status) if rendered else None,
        "rendered": rendered,
        "title": title,
        "text": text,
        "items": items,
    }


async def _fail(actor: Any, message: str) -> None:
    fail = getattr(actor, "fail", None)
    if callable(fail):
        try:
            result = fail(status_message=message)
        except TypeError:
            result = fail(message)
        if hasattr(result, "__await__"):
            await result


async def run() -> None:
    from apify import Actor

    async with Actor:
        raw = await Actor.get_input()
        try:
            jobs, timeout = parse_input(raw)
            api_key = raw.get("adscrawlApiKey") if isinstance(raw, dict) else None
            if any(job.render for job in jobs) and (not isinstance(api_key, str) or not api_key.strip()):
                raise InputError("adscrawlApiKey is required")
            if isinstance(api_key, str) and len(api_key) > 1024:
                raise InputError("adscrawlApiKey is invalid")
        except InputError as exc:
            await Actor.push_data({"error": "invalid_input", "message": str(exc)})
            await _fail(Actor, "Invalid actor input")
            return

        limits = httpx.Limits(max_connections=4, max_keepalive_connections=4)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), limits=limits, headers={"user-agent": "adscrawl-apify-actor/0.1"}
        ) as client:
            for job in jobs:
                try:
                    if job.render:
                        final_url, html, status = await rendered_fetch(client, job.url, api_key.strip())
                    else:
                        final_url, html, status = await ordinary_fetch(client, job.url)
                    if status >= 400:
                        await Actor.push_data({"url": job.url, **safe_error(status, html)})
                    else:
                        await Actor.push_data(
                            extract(
                                job.url,
                                final_url,
                                html,
                                job.selector,
                                job.render,
                                status,
                                status if job.render else None,
                            )
                        )
                except AdsCrawlAPIError as exc:
                    log.warning("Request failed for one input (%s)", "api_error")
                    error = safe_error(exc.status)
                    if exc.code:
                        error["code"] = exc.code
                    await Actor.push_data({"url": job.url, **error})
                except (httpx.HTTPError, InputError, RuntimeError):
                    log.warning("Request failed for one input (%s)", "request_error")
                    await Actor.push_data({"url": job.url, **safe_error()})


if __name__ == "__main__":
    asyncio.run(run())
