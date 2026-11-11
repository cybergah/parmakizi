"""Site definitions and the async checking engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources
from typing import Iterable, Optional
from urllib.parse import quote

import httpx

from . import __version__

USER_AGENT = (
    f"Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 parmakizi/{__version__}"
)


class Status(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INVALID = "invalid"  # username is not valid on this site, not checked
    UNKNOWN = "unknown"  # the site answered in a way the rule does not cover
    ERROR = "error"  # network error / timeout


@dataclass(frozen=True)
class Site:
    """One platform and the rule used to decide whether a profile exists.

    detect:
      "status"  -> profile exists on HTTP 200, missing on any status in ``missing_status``
      "message" -> profile missing if the whole (stripped) body equals one of
                   ``missing_body`` or contains one of ``missing_text``;
                   exists if it contains one of ``present_text`` (or, when no
                   ``present_text`` is given, on any 2xx without a missing marker)
    """

    name: str
    url: str  # public profile URL shown to the user, with {u}
    detect: str = "status"
    probe: Optional[str] = None  # URL actually requested (an API), defaults to url
    missing_status: tuple = (404, 410)
    missing_text: tuple = ()
    missing_body: tuple = ()
    present_text: tuple = ()
    regex: Optional[str] = None  # usernames the site accepts
    tags: tuple = ()
    claimed: Optional[str] = None  # a username known to exist (for --self-test)
    headers: dict = field(default_factory=dict, hash=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Site":
        return cls(
            name=d["name"],
            url=d["url"],
            detect=d.get("detect", "status"),
            probe=d.get("probe"),
            missing_status=tuple(d.get("missing_status", (404, 410))),
            missing_text=tuple(d.get("missing_text", ())),
            missing_body=tuple(d.get("missing_body", ())),
            present_text=tuple(d.get("present_text", ())),
            regex=d.get("regex"),
            tags=tuple(d.get("tags", ())),
            claimed=d.get("claimed"),
            headers=dict(d.get("headers", {})),
        )

    def accepts(self, username: str) -> bool:
        return self.regex is None or re.fullmatch(self.regex, username) is not None

    def profile_url(self, username: str) -> str:
        return self.url.replace("{u}", quote(username, safe="@._-"))

    def probe_url(self, username: str) -> str:
        return (self.probe or self.url).replace("{u}", quote(username, safe="@._-"))


@dataclass
class Result:
    site: str
    username: str
    status: Status
    url: str
    http_status: Optional[int] = None
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["status"] = self.status.value
        return d


def load_sites(path: Optional[str] = None) -> list[Site]:
    """Load site rules from a JSON file (default: the bundled sites.json)."""
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(resources.files("parmakizi").joinpath("sites.json").read_text("utf-8"))
    return [Site.from_dict(d) for d in data["sites"]]


def filter_sites(sites: Iterable[Site], names: Iterable[str] = (), tags: Iterable[str] = ()) -> list[Site]:
    names = {n.lower() for n in names}
    tags = {t.lower() for t in tags}
    out = []
    for s in sites:
        if names and s.name.lower() not in names:
            continue
        if tags and not tags.intersection(t.lower() for t in s.tags):
            continue
        out.append(s)
    return out


def classify(site: Site, status_code: int, body: str) -> Status:
    """Decide FOUND / NOT_FOUND / UNKNOWN from an HTTP response. Pure function."""
    if site.detect == "status":
        if status_code == 200:
            return Status.FOUND
        if status_code in site.missing_status:
            return Status.NOT_FOUND
        return Status.UNKNOWN

    if site.detect == "message":
        if status_code in site.missing_status:
            return Status.NOT_FOUND
        if site.missing_body and body.strip() in site.missing_body:
            return Status.NOT_FOUND
        if any(t in body for t in site.missing_text):
            return Status.NOT_FOUND
        if site.present_text:
            return Status.FOUND if any(t in body for t in site.present_text) else Status.UNKNOWN
        return Status.FOUND if 200 <= status_code < 300 else Status.UNKNOWN

    raise ValueError(f"{site.name}: unknown detect method {site.detect!r}")


async def check_site(client: httpx.AsyncClient, site: Site, username: str) -> Result:
    url = site.profile_url(username)
    if not site.accepts(username):
        return Result(site.name, username, Status.INVALID, url)

    start = time.perf_counter()
    try:
        resp = await client.get(site.probe_url(username), headers=site.headers or None)
        # Only read as much of the body as we need to look for markers.
        body = resp.text[:200_000] if site.detect == "message" else ""
        status = classify(site, resp.status_code, body)
        return Result(
            site.name,
            username,
            status,
            url,
            http_status=resp.status_code,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    except httpx.HTTPError as e:
        return Result(
            site.name,
            username,
            Status.ERROR,
            url,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
            error=type(e).__name__,
        )


def make_client(timeout: float = 15.0, proxy: Optional[str] = None, transport=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        proxy=proxy,
        transport=transport,
    )


async def search(
    username: str,
    sites: Iterable[Site],
    *,
    client: Optional[httpx.AsyncClient] = None,
    concurrency: int = 20,
    timeout: float = 15.0,
    proxy: Optional[str] = None,
    on_result=None,
) -> list[Result]:
    """Check ``username`` on every site. Calls ``on_result(result)`` as each one finishes."""
    sem = asyncio.Semaphore(max(1, concurrency))
    own_client = client is None
    client = client or make_client(timeout=timeout, proxy=proxy)

    async def one(site: Site) -> Result:
        async with sem:
            r = await check_site(client, site, username)
        if on_result:
            on_result(r)
        return r

    try:
        return list(await asyncio.gather(*(one(s) for s in sites)))
    finally:
        if own_client:
            await client.aclose()
