"""Command-line interface: ``parmakizi <username> [...]``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .core import Result, Status, filter_sites, load_sites, search
from .selftest import self_test

BANNER = r"""
  ┌─┐┌─┐┬─┐┌┬┐┌─┐┬┌─  ┬┌─┐┬
  ├─┘├─┤├┬┘│││├─┤├┴┐  │┌─┘│
  ┴  ┴ ┴┴└─┴ ┴┴ ┴┴ ┴  ┴└─┘┴   v{v} · cybergah
"""

DISCLAIMER = (
    "Only public profile pages are checked. Use this tool for legal research, "
    "self-audits and authorized investigations only."
)


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.enabled else s

    def green(self, s):
        return self._c("32;1", s)

    def red(self, s):
        return self._c("31", s)

    def yellow(self, s):
        return self._c("33", s)

    def dim(self, s):
        return self._c("2", s)

    def bold(self, s):
        return self._c("1", s)


SYMBOL = {
    Status.FOUND: "[+]",
    Status.NOT_FOUND: "[-]",
    Status.INVALID: "[~]",
    Status.UNKNOWN: "[?]",
    Status.ERROR: "[!]",
}


def format_result(r: Result, st: Style) -> str:
    sym = SYMBOL[r.status]
    if r.status is Status.FOUND:
        return f"{st.green(sym)} {r.site:<28} {r.url}"
    if r.status is Status.NOT_FOUND:
        return st.dim(f"{sym} {r.site:<28} not found")
    if r.status is Status.INVALID:
        return st.dim(f"{sym} {r.site:<28} username not valid here")
    if r.status is Status.ERROR:
        return st.red(f"{sym} {r.site:<28} {r.error}")
    return st.yellow(f"{sym} {r.site:<28} unclear answer (HTTP {r.http_status})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parmakizi",
        description="Find where a username exists across the public web. " + DISCLAIMER,
    )
    p.add_argument("usernames", nargs="*", help="one or more usernames to look up")
    p.add_argument("-s", "--site", action="append", default=[], metavar="NAME", help="only check this site (repeatable)")
    p.add_argument("-t", "--tag", action="append", default=[], metavar="TAG", help="only check sites with this tag, e.g. dev, security, social")
    p.add_argument("-a", "--all", action="store_true", help="also print sites where the username was not found")
    p.add_argument("--json", metavar="FILE", help="write results to a JSON file ('-' for stdout)")
    p.add_argument("--csv", metavar="FILE", help="write results to a CSV file")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds (default 15)")
    p.add_argument("--concurrency", type=int, default=20, help="parallel requests (default 20)")
    p.add_argument("--proxy", metavar="URL", help="route traffic through a proxy, e.g. socks5://127.0.0.1:9050 for Tor")
    p.add_argument("--sites-file", metavar="FILE", help="use a custom sites.json")
    p.add_argument("--list-sites", action="store_true", help="list supported sites and exit")
    p.add_argument("--self-test", action="store_true", help="verify every site rule still works and exit")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    p.add_argument("-q", "--quiet", action="store_true", help="no banner, only results")
    p.add_argument("-V", "--version", action="version", version=f"parmakizi {__version__}")
    return p


def _write_outputs(results: list[Result], json_path: Optional[str], csv_path: Optional[str]) -> None:
    rows = [r.to_dict() for r in results]
    if json_path:
        text = json.dumps(rows, ensure_ascii=False, indent=2)
        if json_path == "-":
            print(text)
        else:
            with open(json_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
    if csv_path:
        fields = ["username", "site", "status", "url", "http_status", "elapsed_ms", "error"]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k) for k in fields})


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows pipes default to a legacy code page: never crash on a symbol.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    color = not args.no_color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    st = Style(color)
    if color and os.name == "nt":
        os.system("")  # enable ANSI escape codes on Windows terminals

    try:
        sites = filter_sites(load_sites(args.sites_file), args.site, args.tag)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: could not load sites: {e}", file=sys.stderr)
        return 2
    if not sites:
        print("error: no sites match the given --site/--tag filters", file=sys.stderr)
        return 2

    if args.list_sites:
        for s in sites:
            print(f"{s.name:<28} {', '.join(s.tags):<20} {s.url}")
        print(st.dim(f"\n{len(sites)} sites"))
        return 0

    if args.self_test:
        health = asyncio.run(self_test(sites, timeout=args.timeout, proxy=args.proxy))
        broken = [h for h in health if h.broken]
        skipped = [h for h in health if not h.ok and not h.broken]
        order = {"BAD": 0, "SKIP": 1, "OK ": 2}
        rows = [("OK " if h.ok else "BAD" if h.broken else "SKIP", h) for h in health]
        for label, h in sorted(rows, key=lambda r: (order[r[0]], r[1].site)):
            line = f"{label:<4} {h.site:<28} {h.reason}"
            print(st.green(line) if label == "OK " else st.red(line) if label == "BAD" else st.yellow(line))
        ok = len(health) - len(broken) - len(skipped)
        print(f"\n{ok}/{len(health)} rules healthy, {len(broken)} broken, {len(skipped)} inconclusive (rate limit / network)")
        return 1 if broken else 0

    if not args.usernames:
        build_parser().print_usage(sys.stderr)
        print("error: give at least one username (or --list-sites / --self-test)", file=sys.stderr)
        return 2

    to_stdout_json = args.json == "-"
    show = not to_stdout_json
    if show and not args.quiet:
        print(st.green(BANNER.format(v=__version__)))
        print(st.dim(DISCLAIMER) + "\n")

    all_results: list[Result] = []
    for username in args.usernames:
        if show:
            print(st.bold(f"» {username}") + st.dim(f"  ({len(sites)} sites)"))

        def on_result(r: Result) -> None:
            if show and (args.all or r.status not in (Status.NOT_FOUND, Status.INVALID)):
                print("  " + format_result(r, st), flush=True)

        try:
            results = asyncio.run(
                search(
                    username,
                    sites,
                    concurrency=args.concurrency,
                    timeout=args.timeout,
                    proxy=args.proxy,
                    on_result=on_result,
                )
            )
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except ImportError as e:  # e.g. socks proxy without httpx[socks]
            print(f"error: {e}\nhint: pip install 'httpx[socks]' for SOCKS/Tor proxies", file=sys.stderr)
            return 2

        found = sum(r.status is Status.FOUND for r in results)
        if show:
            print(st.dim(f"  {found} found · {len(results)} checked\n"))
        all_results.extend(sorted(results, key=lambda r: r.site.lower()))

    _write_outputs(all_results, args.json, args.csv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
