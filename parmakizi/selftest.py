"""Check that every site rule still works.

For each site we look up a username known to exist (``claimed``) and a random
one that almost certainly does not. A rule is healthy when the first is FOUND
and the second is NOT_FOUND.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

from .core import Result, Site, Status, check_site, make_client


@dataclass
class Health:
    site: str
    ok: bool
    claimed: Optional[Result]
    random: Result

    @property
    def reason(self) -> str:
        if self.ok:
            return "ok"
        parts = []
        if self.claimed is None:
            parts.append("no claimed username")
        elif self.claimed.status is not Status.FOUND:
            parts.append(f"claimed={self.claimed.status.value}({self.claimed.http_status or self.claimed.error})")
        if self.random.status is not Status.NOT_FOUND:
            parts.append(f"random={self.random.status.value}({self.random.http_status or self.random.error})")
        return ", ".join(parts)


def random_username() -> str:
    # letters only, starts with a letter, 14 chars: valid almost everywhere
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(14))


async def self_test(
    sites: Iterable[Site],
    *,
    client: Optional[httpx.AsyncClient] = None,
    concurrency: int = 10,
    timeout: float = 20.0,
    proxy: Optional[str] = None,
) -> list[Health]:
    sem = asyncio.Semaphore(max(1, concurrency))
    own = client is None
    client = client or make_client(timeout=timeout, proxy=proxy)
    rnd = random_username()

    async def one(site: Site) -> Health:
        async with sem:
            claimed = await check_site(client, site, site.claimed) if site.claimed else None
            random = await check_site(client, site, rnd)
        ok = claimed is not None and claimed.status is Status.FOUND and random.status is Status.NOT_FOUND
        return Health(site.name, ok, claimed, random)

    try:
        return list(await asyncio.gather(*(one(s) for s in sites)))
    finally:
        if own:
            await client.aclose()
