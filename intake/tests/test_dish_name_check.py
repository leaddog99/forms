"""Typo guard at dish creation (input/pipeline/dish_name_check.py), faked model."""
from types import SimpleNamespace

from input.pipeline import dish_name_check as d

quiet = lambda *_a, **_k: None  # noqa: E731


def _fake(reply):
    def create(**_kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    return create


def test_typo_gives_the_suggestion():
    r = d.check("Seared Scallope", create=_fake('{"typo": true, "suggestion": "Seared Scallops"}'), log=quiet)
    assert r and r["suggestion"] == "Seared Scallops" and r["method"] == d.METHOD_ID


def test_a_fine_name_passes():
    assert d.check("Kolokithopita", create=_fake('{"typo": false}'), log=quiet) is None


def test_case_or_accent_only_change_is_not_a_typo():
    assert d.check("etouffee", create=_fake('{"typo": true, "suggestion": "Étouffée"}'), log=quiet) is None
    assert d.check("po boy sandwich", create=_fake('{"typo": true, "suggestion": "Po\'Boy Sandwich"}'), log=quiet) is None


def test_a_failed_call_never_blocks():
    def boom(**_kw):
        raise RuntimeError("no network")
    assert d.check("Seared Scallope", create=boom, log=quiet) is None
    assert d.check("Seared Scallope", create=_fake("not json at all"), log=quiet) is None
