"""The search request counter: every provider request is one row, and counting never breaks a search."""
import os
import tempfile

from input.pipeline import search_usage as su


class _Resp:
    def __init__(self, status, results=0, headers=None):
        self.status_code, self._n, self.headers = status, results, headers or {}

    def json(self):
        return {"web": {"results": [{}] * self._n}}


def _db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "t.db")


def test_each_request_is_one_row_and_the_response_is_untouched(monkeypatch):
    db = _db()
    monkeypatch.setattr(su, "record", lambda provider, **kw: _orig(provider, db_path=db, **kw))
    answers = iter([_Resp(200, 20, {"X-RateLimit-Limit": "1, 2000", "X-RateLimit-Remaining": "0, 1543"}),
                    _Resp(429), _Resp(200, 7)])
    get = su.counting_get("brave", lambda url, **kw: next(answers))
    kw = dict(params={"q": "Dakos", "offset": 0, "country": "GR", "search_lang": "el"}, headers={}, timeout=5)
    first = get("u", **kw)
    assert first.status_code == 200 and len(first.json()["web"]["results"]) == 20
    get("u", **kw), get("u", **kw)
    s = su.summary(1, db_path=db)
    assert s["total"] == 3 and s["answered"] == 2
    assert s["days"][0]["rate_limited"] == 1
    assert s["quota"]["remaining"] == "0, 1543"


def test_a_request_that_raises_is_counted_and_still_raises(monkeypatch):
    db = _db()
    monkeypatch.setattr(su, "record", lambda provider, **kw: _orig(provider, db_path=db, **kw))

    def boom(url, **kw):
        raise TimeoutError("no answer")
    get = su.counting_get("brave", boom)
    try:
        get("u", params={"q": "x"}, headers={}, timeout=1)
        raise AssertionError("should have raised")
    except TimeoutError:
        pass
    s = su.summary(1, db_path=db)
    assert s["total"] == 1 and s["answered"] == 0 and s["days"][0]["other"] == 1


def test_a_counter_that_cannot_write_does_not_break_the_search():
    su.record("brave", status=200, db_path=os.path.join(tempfile.mkdtemp(), "no", "such", "dir", "x.db"))


_orig = su.record
