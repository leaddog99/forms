"""Behaviour tests for the page-fetch ladder (direct -> paid unblocker -> Wayback).

Run:
    python -m pytest to_markdown/tests/ -q            # if pytest is installed
    python -m to_markdown.tests.test_fetch_ladder     # no-pytest fallback runner
    python -m to_markdown.tests.test_fetch_ladder --record   # rewrite the golden file

No network, no database, no sleeping. `requests.get` is replaced by a router
that answers as the origin, the unblocker proxy or archive.org; settings come
back as their defaults; ledger rows are captured instead of written.

Each scenario fetches one or more URLs in ONE simulated run and records what
the ladder did: which requests it made, what it wrote to the ledger, how long
it slept, and what it returned or raised. The whole record is compared with
`fetch_ladder_golden.json`. A change to the ladder that alters behaviour shows
up as a diff of that file, which is then either a bug or a decision to record.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import requests  # noqa: E402

from to_markdown import fetch as F  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "fetch_ladder_golden.json")

REAL = ("<html><head><script type='application/ld+json'>{\"@type\":\"Recipe\"}</script></head>"
        "<body><article>" + "recipe text " * 2000 + "</article></body></html>")
SHELL = "<html><body><div id='app'>" + "nav item " * 2500 + "</div></body></html>"   # big, no structure
STUB = "<html><body>Just a moment...</body></html>"                                    # challenge page
PARKED = '<html><script>window.location.href="/lander"</script></html>'


def _resp(url: str, status: int, body: str, headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode("utf-8")
    r.encoding = "utf-8"
    r.url = url
    r.headers.update(headers or {})
    return r


class Sim:
    """One simulated run: routes requests.get, captures ledger rows and sleeps."""

    def __init__(self, origin, *, unblocker=None, wayback=None, creds=True, paid_first=()):
        self.origin, self.unblocker, self.wayback = origin, unblocker, wayback
        self.creds, self.paid_first = creds, set(paid_first)
        self.calls: list = []
        self.ledger: list = []
        self.sleeps: list = []

    # -- the fake network ------------------------------------------------
    def get(self, url, **kw):
        ua = (kw.get("headers") or {}).get("User-Agent", "")
        if "archive.org/wayback/available" in url:
            target = kw["params"]["url"]
            self.calls.append("wayback?")
            ts = self.wayback(target) if self.wayback else None
            snap = {"closest": {"timestamp": ts, "url": "http://web.archive.org/x"}} if ts else {}
            return _resp(url, 200, json.dumps({"archived_snapshots": snap}))
        if "web.archive.org/web/" in url:
            self.calls.append("wayback")
            return _resp(url, 200, REAL)
        if kw.get("proxies"):
            self.calls.append("unblocker" + ("+render" if "x-oxylabs-render" in (kw.get("headers") or {}) else ""))
            status, body = self.unblocker(url)
            return _resp(url, status, body)
        self.calls.append("direct:" + ("browser" if "Chrome" in ua else "bot"))
        out = self.origin(url, "browser" if "Chrome" in ua else "bot")
        if isinstance(out, Exception):
            raise out
        status, body = out
        return _resp(url, status, body)

    # -- run it ----------------------------------------------------------
    def run(self, steps) -> list:
        from input.pipeline import acquisition, domains_lib, system_config
        saved = (requests.get, acquisition.record, system_config.get_setting,
                 domains_lib.get_paid_first_hosts, F.time.sleep, F.random.uniform, dict(os.environ))
        requests.get = self.get
        acquisition.record = lambda url, technique, **kw: self.ledger.append(
            f"{kw.get('rung')}:{technique}:{'ok' if kw.get('ok') else 'lost'}"
            + (f":{kw['notes']}" if kw.get("notes") else ""))
        system_config.get_setting = lambda key, default=None, **kw: default
        domains_lib.get_paid_first_hosts = lambda *a, **kw: self.paid_first
        F.time.sleep = lambda s: self.sleeps.append(round(s))
        F.random.uniform = lambda a, b: 0
        for k in ("UNBLOCKER_PROXY_USER", "UNBLOCKER_PROXY_PASS", "UNBLOCKER_API_KEY"):
            os.environ.pop(k, None)
        os.environ["UNBLOCKER_PROVIDER"] = "oxylabs"
        if self.creds:
            os.environ["UNBLOCKER_PROXY_USER"] = "u"
            os.environ["UNBLOCKER_PROXY_PASS"] = "p"
        F.reset_run_state()
        out = []
        try:
            for url, kw in steps:
                self.calls, self.ledger, self.sleeps = [], [], []
                try:
                    resp, meta = F.fetch_with_full_fallback(url, **kw)
                    result = "returned " + meta["source"] + ("" if not F.blocked_reason(resp) else " (blocked page)")
                except Exception as e:      # noqa: BLE001 - the ladder's failure IS the result
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    result = f"raised {type(e).__name__}" + (f" {status}" if status else "")
                out.append({"url": url.rsplit("/", 1)[-1], "result": result, "requests": self.calls,
                            "ledger": self.ledger, "slept": self.sleeps})
        finally:
            (requests.get, acquisition.record, system_config.get_setting,
             domains_lib.get_paid_first_hosts, F.time.sleep, F.random.uniform) = saved[:6]
            os.environ.clear()
            os.environ.update(saved[6])
            F.reset_run_state()
        return out


U = "https://site.test/recipe/"
PAID = {"unblocker": True}
RENDER = {"unblocker": True, "render": True}


def _always(status, body):
    return lambda url, ua=None: (status, body)


def scenarios() -> dict:
    s = {}
    s["direct ok"] = Sim(_always(200, REAL)).run([(U + "a", {})])
    s["direct 404 is terminal"] = Sim(_always(404, ""), unblocker=_always(200, REAL),
                                       wayback=lambda u: "2026").run([(U + "a", PAID)])
    s["bot UA refused, browser UA served"] = Sim(
        lambda url, ua: (200, REAL) if ua == "browser" else (403, "")).run([(U + "a", {})])
    s["bot UA challenged with a 2xx stub, browser UA served"] = Sim(
        lambda url, ua: (200, REAL) if ua == "browser" else (202, STUB)).run([(U + "a", {})])
    s["403, paid tier off -> wayback"] = Sim(_always(403, ""), wayback=lambda u: "2026").run([(U + "a", {})])
    s["403, paid tier off, no snapshot"] = Sim(_always(403, "")).run([(U + "a", {})])
    s["403, paid tier on -> unblocker"] = Sim(_always(403, ""), unblocker=_always(200, REAL)).run([(U + "a", PAID)])
    s["403, paid tier asked for but no credentials"] = Sim(
        _always(403, ""), unblocker=_always(200, REAL), wayback=lambda u: "2026", creds=False).run([(U + "a", PAID)])
    s["challenge stub, paid tier on -> unblocker"] = Sim(
        _always(200, STUB), unblocker=_always(200, REAL)).run([(U + "a", PAID)])
    s["challenge stub, paid tier off -> wayback"] = Sim(
        _always(200, STUB), wayback=lambda u: "2026").run([(U + "a", {})])
    s["unblocker returns a challenge stub"] = Sim(
        _always(403, ""), unblocker=_always(200, STUB), wayback=lambda u: "2026").run([(U + "a", PAID)])
    s["parked domain never pays"] = Sim(
        _always(200, PARKED), unblocker=_always(200, REAL), wayback=lambda u: "2026").run([(U + "a", PAID)])
    s["network error -> unblocker"] = Sim(
        lambda url, ua: requests.ConnectionError("boom"), unblocker=_always(200, REAL)).run([(U + "a", PAID)])
    s["try_wayback off"] = Sim(_always(403, ""), wayback=lambda u: "2026").run([(U + "a", {"try_wayback": False})])
    s["429 pauses once per host"] = Sim(_always(429, ""), unblocker=_always(200, REAL)).run(
        [(U + "a", PAID), (U + "b", PAID), ("https://other.test/c", PAID)])
    hits = {"n": 0}

    def _recovers(url, ua):
        hits["n"] += 1
        return (429, "") if hits["n"] <= 2 else (200, REAL)
    s["429 then served after one pause"] = Sim(_recovers).run([(U + "a", {}), (U + "b", {})])
    s["render: probe carries the recipe"] = Sim(_always(200, REAL), unblocker=_always(200, REAL)).run([(U + "a", RENDER)])
    s["render: probe is a JS shell -> rendered fetch"] = Sim(
        _always(200, SHELL), unblocker=_always(200, REAL)).run([(U + "a", RENDER)])
    s["render: probe is a JS shell, unblocker blocked"] = Sim(
        _always(200, SHELL), unblocker=_always(403, ""), wayback=lambda u: "2026").run([(U + "a", RENDER)])
    s["render: probe refused, unblocker blocked"] = Sim(
        _always(403, ""), unblocker=_always(403, ""), wayback=lambda u: "2026").run([(U + "a", RENDER)])
    s["render: a missing page is terminal and never paid for"] = Sim(
        _always(404, ""), unblocker=_always(200, REAL), wayback=lambda u: "2026").run([(U + "a", RENDER)])
    s["render: 429 pauses before anything is paid for"] = Sim(
        _always(429, ""), unblocker=_always(200, REAL)).run([(U + "a", RENDER)])

    def _proxy_down(url):
        raise requests.ConnectionError("proxy timeout")
    s["render: rendered fetch never reaches the site -> one static try"] = Sim(
        _always(403, ""), unblocker=_proxy_down, wayback=lambda u: "2026").run([(U + "a", RENDER)])
    s["paid-first: unblocker returns a challenge stub"] = Sim(
        _always(429, ""), unblocker=_always(200, STUB), wayback=lambda u: "2026",
        paid_first={"site.test"}).run([(U + "a", {})])
    s["paid-first: unblocker serves"] = Sim(
        _always(429, ""), unblocker=_always(200, REAL), paid_first={"site.test"}).run([(U + "a", {})])
    s["paid-first: unblocker refused -> wayback, never direct"] = Sim(
        _always(429, ""), unblocker=_always(550, ""), wayback=lambda u: "2026",
        paid_first={"site.test"}).run([(U + "a", {})])
    s["paid-first without credentials is inert"] = Sim(
        _always(200, REAL), paid_first={"site.test"}, creds=False).run([(U + "a", {})])
    s["give up on a host after three lost URLs"] = Sim(_always(403, ""), unblocker=_always(550, "")).run(
        [(U + c, PAID) for c in "abcde"])
    s["a missing page does not count toward giving up"] = Sim(
        _always(429, ""), unblocker=_always(404, ""), paid_first={"site.test"}).run([(U + c, {}) for c in "abcde"])
    s["unblocker breaker opens after three origin blocks"] = Sim(
        _always(403, ""), unblocker=_always(403, ""), wayback=lambda u: "2026").run([(U + c, PAID) for c in "abcd"])
    flip = {"n": 0}

    def _third_ok(url, ua):
        flip["n"] += 1
        return (200, REAL) if url.endswith("/c") else (403, "")
    s["a success resets the give-up count"] = Sim(_third_ok).run([(U + c, {}) for c in "abcdef"])
    return s


def test_ladder_matches_golden():
    got = scenarios()
    with open(GOLDEN, encoding="utf-8") as f:
        want = json.load(f)
    assert sorted(got) == sorted(want), "scenario list changed - re-record deliberately"
    for name in want:
        assert got[name] == want[name], (
            f"{name}\n  want {json.dumps(want[name])}\n  got  {json.dumps(got[name])}")


def test_blocked_reason_tiers():
    r = lambda body: _resp(U, 200, body)   # noqa: E731
    assert F.blocked_reason(r(REAL)) is None
    assert "challenge page" in F.blocked_reason(r(STUB))
    assert F.blocked_reason(r(SHELL)) is None                      # big enough to be a page
    assert "thin body" in F.blocked_reason(r("<html>" + "x" * 5000))
    assert F.blocked_reason(r("<html>" + "x" * 5000), strict=True) is None   # verdict needs near-certainty
    assert F.blocked_reason(r(REAL + "challenge-platform")) is None           # passive probe on a real page
    assert "anti-bot vendor" in F.blocked_reason(r(SHELL + "datadome"))
    assert F.blocked_reason(r(SHELL + "datadome"), strict=True) is None
    assert "parked" in F.parked_reason(r(PARKED))
    assert F.parked_reason(r(REAL)) is None


if __name__ == "__main__":
    if "--record" in sys.argv:
        with open(GOLDEN, "w", encoding="utf-8") as f:
            json.dump(scenarios(), f, indent=1)
        print(f"recorded {GOLDEN}")
    else:
        test_blocked_reason_tiers()
        test_ladder_matches_golden()
        print("fetch ladder: all scenarios match the golden record")
