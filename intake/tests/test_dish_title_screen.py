"""The title screen: only `different` is dropped, pins are never screened, and
any failure leaves every candidate in. No network: the model call is faked."""
import json
import types

import pytest

from intake import dish_title_screen as dts


def _reply(verdicts):
    def create(**kw):
        create.kw = kw
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(verdicts))])
    return create


def _entries():
    return [{"url": "https://a.com/veal-cacciatore", "title": "Veal Cacciatore"},
            {"url": "https://b.com/chicken-cacciatore", "title": "Chicken Cacciatore"},
            {"url": "https://c.com/cacciatore", "title": "Cacciatore Recipe"},
            {"url": "https://d.com/pin", "title": "Chicken Cacciatore (pinned)", "_pinned": True}]


VERDICTS = [{"n": 1, "v": "same"}, {"n": 2, "v": "different", "is": "chicken cacciatore"},
            {"n": 3, "v": "unclear"}]


def test_drop_mode_drops_only_different(monkeypatch):
    monkeypatch.setattr(dts, "mode", lambda: "drop")
    monkeypatch.setattr(dts, "_about", lambda name: "")
    create = _reply(VERDICTS)
    kept, dropped, s = dts.screen("Veal Cacciatore", _entries(), _create=create)
    assert [e["title"] for e in dropped] == ["Chicken Cacciatore"]
    assert dropped[0]["_dropped_reason"] == "off-dish-title: chicken cacciatore"
    assert [e["title"] for e in kept] == ["Veal Cacciatore", "Cacciatore Recipe", "Chicken Cacciatore (pinned)"]
    assert (s["same"], s["different"], s["unclear"], s["judged"]) == (1, 1, 1, 3)
    # the pin was never shown to the model
    assert "pinned" not in create.kw["messages"][0]["content"]


def test_report_mode_drops_nothing_but_says_what_it_would(monkeypatch):
    monkeypatch.setattr(dts, "mode", lambda: "report")
    monkeypatch.setattr(dts, "_about", lambda name: "")
    kept, dropped, s = dts.screen("Veal Cacciatore", _entries(), _create=_reply(VERDICTS))
    assert dropped == [] and len(kept) == 4
    assert s["would_drop"] == [("Chicken Cacciatore", "chicken cacciatore")]
    assert all("_dropped_reason" not in e for e in kept)


def test_off_mode_makes_no_call(monkeypatch):
    monkeypatch.setattr(dts, "mode", lambda: "off")
    def boom(**kw):
        raise AssertionError("no call in off mode")
    kept, dropped, s = dts.screen("Veal Cacciatore", _entries(), _create=boom)
    assert len(kept) == 4 and dropped == []


def test_a_result_the_model_skips_stays(monkeypatch):
    monkeypatch.setattr(dts, "mode", lambda: "drop")
    monkeypatch.setattr(dts, "_about", lambda name: "")
    kept, dropped, s = dts.screen("Veal Cacciatore", _entries(), _create=_reply([{"n": 1, "v": "same"}]))
    assert dropped == [] and len(kept) == 4 and s["judged"] == 1


def test_unparseable_reply_raises_so_the_caller_keeps_everything(monkeypatch):
    monkeypatch.setattr(dts, "mode", lambda: "drop")
    monkeypatch.setattr(dts, "_about", lambda name: "")
    def chatty(**kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="I think most are fine.")])
    with pytest.raises(ValueError):
        dts.screen("Veal Cacciatore", _entries(), _create=chatty)
