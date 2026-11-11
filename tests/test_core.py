"""Offline tests: no request ever leaves the machine (httpx.MockTransport)."""

import asyncio
import contextlib
import io
import json
import os
import re
import tempfile
import unittest

import httpx

from parmakizi.cli import main
from parmakizi.core import Site, Status, check_site, classify, filter_sites, load_sites, make_client, search


def site(**kw) -> Site:
    base = {"name": "Example", "url": "https://example.test/{u}"}
    base.update(kw)
    return Site.from_dict(base)


class ClassifyTests(unittest.TestCase):
    def test_status_rule(self):
        s = site()
        self.assertIs(classify(s, 200, ""), Status.FOUND)
        self.assertIs(classify(s, 404, ""), Status.NOT_FOUND)
        self.assertIs(classify(s, 410, ""), Status.NOT_FOUND)
        self.assertIs(classify(s, 403, ""), Status.UNKNOWN)
        self.assertIs(classify(s, 500, ""), Status.UNKNOWN)

    def test_custom_missing_status(self):
        s = site(missing_status=[400, 404])
        self.assertIs(classify(s, 400, ""), Status.NOT_FOUND)

    def test_message_rule_with_present_text(self):
        s = site(detect="message", missing_text=["no such user"], present_text=['"id"'])
        self.assertIs(classify(s, 200, '{"id": 1}'), Status.FOUND)
        self.assertIs(classify(s, 200, "Sorry, no such user"), Status.NOT_FOUND)
        self.assertIs(classify(s, 200, "something else"), Status.UNKNOWN)
        self.assertIs(classify(s, 404, ""), Status.NOT_FOUND)

    def test_message_rule_without_present_text(self):
        s = site(detect="message", missing_text=["not found"])
        self.assertIs(classify(s, 200, "a profile"), Status.FOUND)
        self.assertIs(classify(s, 200, "page not found"), Status.NOT_FOUND)
        self.assertIs(classify(s, 503, "a profile"), Status.UNKNOWN)

    def test_missing_body_is_an_exact_match(self):
        s = site(detect="message", missing_body=["null"], present_text=['"id"'])
        self.assertIs(classify(s, 200, "null\n"), Status.NOT_FOUND)
        # "null" inside a real profile must not count as missing
        self.assertIs(classify(s, 200, '{"id": "x", "about": null}'), Status.FOUND)

    def test_unknown_detect_method(self):
        with self.assertRaises(ValueError):
            classify(site(detect="magic"), 200, "")


class SiteTests(unittest.TestCase):
    def test_urls_and_regex(self):
        s = site(probe="https://api.example.test/users/{u}", regex=r"^[a-z]+$")
        self.assertEqual(s.profile_url("bob"), "https://example.test/bob")
        self.assertEqual(s.probe_url("bob"), "https://api.example.test/users/bob")
        self.assertTrue(s.accepts("bob"))
        self.assertFalse(s.accepts("Bob1"))

    def test_username_is_url_encoded(self):
        s = site()
        self.assertEqual(s.profile_url("a b/c"), "https://example.test/a%20b%2Fc")

    def test_filter_by_name_and_tag(self):
        sites = [site(name="A", tags=["dev"]), site(name="B", tags=["social"]), site(name="C", tags=["dev", "ai"])]
        self.assertEqual([s.name for s in filter_sites(sites, names=["b"])], ["B"])
        self.assertEqual([s.name for s in filter_sites(sites, tags=["DEV"])], ["A", "C"])
        self.assertEqual(len(filter_sites(sites)), 3)


class BundledSitesTests(unittest.TestCase):
    def test_bundled_rules_are_well_formed(self):
        sites = load_sites()
        self.assertGreaterEqual(len(sites), 25)
        names = [s.name for s in sites]
        self.assertEqual(len(names), len(set(names)), "duplicate site names")
        for s in sites:
            with self.subTest(site=s.name):
                self.assertIn("{u}", s.url)
                self.assertTrue(s.url.startswith("https://"))
                if s.probe:
                    self.assertIn("{u}", s.probe)
                    self.assertTrue(s.probe.startswith("https://"))
                self.assertIn(s.detect, ("status", "message"))
                if s.detect == "message":
                    self.assertTrue(s.missing_text or s.missing_body or s.missing_status)
                self.assertTrue(s.claimed, "every rule needs a claimed username for --self-test")
                self.assertTrue(s.accepts(s.claimed), "claimed username must match the regex")
                if s.regex:
                    re.compile(s.regex)
                self.assertTrue(s.tags)


def mock_client(handler) -> httpx.AsyncClient:
    return make_client(transport=httpx.MockTransport(handler))


class EngineTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_check_site_found_and_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200 if request.url.path == "/alice" else 404)

        async def go():
            async with mock_client(handler) as c:
                a = await check_site(c, site(), "alice")
                b = await check_site(c, site(), "nobody")
            return a, b

        a, b = self.run_async(go())
        self.assertIs(a.status, Status.FOUND)
        self.assertEqual(a.http_status, 200)
        self.assertEqual(a.url, "https://example.test/alice")
        self.assertIs(b.status, Status.NOT_FOUND)

    def test_probe_url_is_requested_but_profile_url_reported(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200)

        async def go():
            async with mock_client(handler) as c:
                return await check_site(c, site(probe="https://api.example.test/u/{u}"), "alice")

        r = self.run_async(go())
        self.assertEqual(seen, ["https://api.example.test/u/alice"])
        self.assertEqual(r.url, "https://example.test/alice")

    def test_invalid_username_makes_no_request(self):
        def handler(request):
            raise AssertionError("no request expected")

        async def go():
            async with mock_client(handler) as c:
                return await check_site(c, site(regex=r"^[a-z]+$"), "Not-Valid!")

        self.assertIs(self.run_async(go()).status, Status.INVALID)

    def test_network_error_becomes_error_status(self):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        async def go():
            async with mock_client(handler) as c:
                return await check_site(c, site(), "alice")

        r = self.run_async(go())
        self.assertIs(r.status, Status.ERROR)
        self.assertEqual(r.error, "ConnectError")

    def test_search_runs_all_sites_and_calls_back(self):
        sites = [site(name=f"S{i}", url=f"https://s{i}.test/{{u}}") for i in range(10)]

        def handler(request):
            return httpx.Response(200 if request.url.host in ("s1.test", "s7.test") else 404)

        got = []

        async def go():
            async with mock_client(handler) as c:
                return await search("alice", sites, client=c, concurrency=3, on_result=got.append)

        results = self.run_async(go())
        self.assertEqual(len(results), 10)
        self.assertEqual(len(got), 10)
        self.assertEqual(sorted(r.site for r in results if r.status is Status.FOUND), ["S1", "S7"])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_sites(self):
        code, out, _ = self.run_cli("--list-sites", "--no-color")
        self.assertEqual(code, 0)
        self.assertIn("GitHub", out)

    def test_no_username_is_an_error(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("username", err)

    def test_unknown_filter_is_an_error(self):
        code, _, err = self.run_cli("alice", "--site", "does-not-exist")
        self.assertEqual(code, 2)

    def test_custom_sites_file_and_outputs(self):
        # A sites file that points at an unroutable address: every check errors
        # out quickly, which is enough to exercise the output writers.
        data = {"sites": [{"name": "Local", "url": "https://127.0.0.1:9/{u}", "claimed": "x", "tags": ["test"]}]}
        with tempfile.TemporaryDirectory() as d:
            sites = os.path.join(d, "sites.json")
            js = os.path.join(d, "out.json")
            cs = os.path.join(d, "out.csv")
            with open(sites, "w", encoding="utf-8") as f:
                json.dump(data, f)
            code, out, _ = self.run_cli("alice", "--sites-file", sites, "--json", js, "--csv", cs, "--timeout", "2", "-q", "--no-color")
            self.assertEqual(code, 0)
            with open(js, encoding="utf-8") as f:
                rows = json.load(f)
            self.assertEqual(rows[0]["site"], "Local")
            self.assertEqual(rows[0]["username"], "alice")
            self.assertIn(rows[0]["status"], ("error", "unknown"))
            with open(cs, encoding="utf-8") as f:
                self.assertTrue(f.readline().startswith("username,site,status"))


if __name__ == "__main__":
    unittest.main()
