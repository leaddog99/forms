"""Tests for intake/youtube_source.py — the YouTube ladder.

Run:
    python -m pytest intake/tests/ -q          # if pytest is installed
    python -m intake.tests.test_youtube_source  # no-pytest fallback runner

No network: the watch-page and transcript fetchers are injected. The live
check (Fallow, "Michelin Beef Pie at Home", 2026-10-17) is in the module
docstring's history, not here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intake import youtube_source as ys  # noqa: E402

URL = "https://www.youtube.com/watch?v=XE6JvrChQ_Q&t=42s"

# A trimmed copy of Fallow's description: the shape that must pass rung 1.
FALLOW_DESC = """This is a Michelin-level beef & ale pie made at home.
INGREDIENTS
For the Braised Beef
1.5–2 kg beef short ribs (on the bone)
1–1.2 kg beef cheeks
Plain flour, for dusting
2 onions, roughly chopped
3 carrots, roughly chopped
4 cloves garlic, crushed
2 tbsp tomato purée
½ bottle red wine
2 pints Guinness (or other stout)
~75 ml Worcestershire sauce
50 g plain flour
METHOD
1. Braise the Beef
Lightly dust the beef cheeks and short ribs in flour, shaking off any excess.
Heat a heavy-bottomed pan over low–medium heat with oil.
Brown the beef gently on all sides until deeply golden. Work in batches.
2. Deglaze & Build the Braise
Deglaze the pan with red wine and Guinness.
Reduce by half over a gentle simmer (about 10 minutes).
youtube: @fallowchefs
instagram: @fallowchefs
"""

# Fallow's Butter Chicken (8V6Krk8DDuc, 2026-10-17): starred headings, "Step by
# Step" instead of Method, unnumbered steps, two sub-recipes. Fell to the
# transcript rung before the heading regex learned "step by step".
STARRED_DESC = """We brought in chef Chet Sharma to show us how Butter Chicken should really be done.

Video Chapters
00:00 - Intro
3:47 - Marinade

*Recipe:*

*Chicken Tikka*(marinated chicken)

*Ingredients*
30g Mustard Oil
30g Kashmiri Chilli Powder
15g Coriander Powder
200g Hang Yoghurt

*Step by Step*
Mix the chicken with the garlic ginger paste, salt and lemon juice and let it marinade for 30 minutes.
Drain the juices released by the chicken.
Grill the chicken over charcoal until cooked and light charred around the edges.

*Makhni Sauce*

*Ingredients*
30g Ghee
5g Cumin Seeds
400g Bombay Onions / Shallots, sliced

*Step by Step*
Heat the oil and add the cumin seeds, fry them until they crackle.
Add the butter, cream, honey and kasoori methi and blend everything until smooth.
"""

LINK_ONLY_DESC = """The full recipe is on my blog: https://www.example-kitchen.com/recipes/beef-pie
Follow me: https://www.instagram.com/examplekitchen
My pans: https://amzn.to/3abcDEF
Music: https://www.epidemicsound.com/referral/xyz
"""

CHATTY_DESC = "Had the best day cooking with my nan. Subscribe for more!\nhttps://www.youtube.com/@nan"

# Cooking Con Claudia's carne asada (C5qcRGzyx4I, 2026-10-19): the ingredient
# list is written, the method is spoken, and every link is a shop. The ladder
# sent the curator to geni.us (her cookbook) and the URL door said the
# description "does not hold the recipe".
INGREDIENTS_ONLY_DESC = """One simple ingredient takes this carne asada from good to unforgettable.

Ingredients:
* 3-4 lbs flap meat, skirt steak, ranchera, or thinly sliced chuck steak
* 7 oz Mexican beer
* Juice of 2 oranges
* Juice of 3 limes
* 1/4 cup soy sauce
* 2 tsp maggi jugo or Worcestershire sauce
* 2 garlic cloves, minced
* 2 tsp garlic powder
* 1 tsp cumin
Roasted Salsa de Nopales
* 2-4 cleaned nopales
* 4 Roma tomatoes
* 3 tomatillos

Get my NEW cookbook, Celebracion!
https://geni.us/Celebracion
Shop all of my favorite kitchen essentials on my Amazon Storefront
https://www.amazon.com/shop/claudiasvlogs
Grab your apron here
https://cookingconclaudia-shop.fourthwall.com/
Linktree : https://linktr.ee/cookingconclaudia
"""

# Ingredients written AND the description says the full recipe is on the blog:
# the written page stays the recipe of record (rung 2), not the transcript.
INGREDIENTS_PLUS_BLOG_DESC = INGREDIENTS_ONLY_DESC.replace(
    "Get my NEW cookbook", "Full recipe with the method on my blog: https://www.example-kitchen.com/recipes/carne-asada\nGet my NEW cookbook")

COMMENT_RECIPE = """Recipe as promised:
Ingredients
200 g flour
2 eggs
300 ml milk
1 tbsp sugar
Method
Whisk everything together and rest for 30 minutes before frying in a hot buttered pan.
Flip once the edges set and serve with lemon and sugar.
"""


def _info(desc, captions=(("en", "asr"),)):
    return ys.VideoInfo(video_id="XE6JvrChQ_Q", title="Michelin Beef Pie at Home", channel="Fallow",
                        channel_id="UCJ901NqoRaXMnIm7aOjLyuA", handle="FallowChefs",
                        description=desc, length_s=1250, caption_tracks=list(captions))


def _transcript(words=600):
    txt = " ".join(["word"] * words)
    return ys.Transcript(text=txt, language="en", is_generated=True, words=words)


def _quiet(_msg):
    pass


def test_signal_recipe_in_description():
    sig = ys.recipe_signal(FALLOW_DESC)
    assert sig.has_recipe, sig
    assert sig.has_ingredient_heading and sig.has_method_heading
    assert sig.ingredient_lines >= 8 and sig.step_lines >= 4


def test_signal_starred_step_by_step_description_passes():
    sig = ys.recipe_signal(STARRED_DESC)
    assert sig.has_recipe, sig
    assert sig.has_method_heading and sig.step_lines >= 4, sig
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(STARRED_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.rung == "description", res.rung


def test_signal_link_only_description_does_not_pass():
    sig = ys.recipe_signal(LINK_ONLY_DESC)
    assert not sig.has_recipe, sig
    assert sig.points_elsewhere


def test_signal_chatty_description_does_not_pass():
    assert not ys.recipe_signal(CHATTY_DESC).has_recipe


def test_recipe_links_drop_social_shop_and_sponsor():
    links = ys.recipe_links(LINK_ONLY_DESC)
    assert links == ["https://www.example-kitchen.com/recipes/beef-pie"], links
    assert ys.recipe_links(CHATTY_DESC) == []


def test_recipe_links_drop_shorteners_bookshops_and_shop_subdomains():
    assert ys.recipe_links(INGREDIENTS_ONLY_DESC) == [], ys.recipe_links(INGREDIENTS_ONLY_DESC)
    assert ys.recipe_links(INGREDIENTS_PLUS_BLOG_DESC) == ["https://www.example-kitchen.com/recipes/carne-asada"]
    for h in ("geni.us", "bit.ly", "shop.example.com", "store.example.com", "example-shop.fourthwall.com",
              "books.example.com", "bookshop.org", "sites.prh.com"):
        assert not ys.is_recipe_host(h), h
    for h in ("example-kitchen.com", "www.notanothercookingshow.tv", "bishopskitchen.com", "seriouseats.com"):
        assert ys.is_recipe_host(h), h


def test_signal_ingredients_only_description_is_half_a_recipe():
    sig = ys.recipe_signal(INGREDIENTS_ONLY_DESC)
    assert sig.has_ingredient_heading and sig.ingredient_lines >= 6 and sig.step_lines == 0, sig
    assert not sig.has_recipe and sig.has_ingredient_list
    assert not ys.recipe_signal(CHATTY_DESC).has_ingredient_list


def test_ladder_rung_1_description_with_transcript_riding_along():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(FALLOW_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.rung == "description"
    assert res.markdown and "## Video description" in res.markdown
    assert "## Transcript" in res.markdown and "corroboration" in res.markdown
    assert res.transcript_words == 600 and res.reject is None
    assert res.url == "https://www.youtube.com/watch?v=XE6JvrChQ_Q"
    assert res.thumbnail.endswith("/XE6JvrChQ_Q/maxresdefault.jpg")
    assert "*Source: " + URL + "*" in res.markdown     # the envelope keeps the given url
    assert res.capture_extra()["rung"] == "description"


def test_ladder_rung_2_link_beats_transcript():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.rung == "link" and res.markdown is None
    assert res.links[0] == "https://www.example-kitchen.com/recipes/beef-pie"


def test_ladder_rung_2_stages_the_transcript_beside_the_link():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.fallback_rung == "transcript" and res.fallback_markdown
    assert "Recipe found in: transcript" in res.fallback_markdown and "the cook speaking" in res.fallback_markdown
    assert res.capture_extra()["fallback_rung"] == "transcript" and res.capture_extra()["rung"] == "link"
    # a creator's comment outranks the transcript as the fallback
    res = ys.ladder(URL, comments=[COMMENT_RECIPE], log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.rung == "link" and res.fallback_rung == "comment" and "## Comment 1" in res.fallback_markdown
    # no transcript, no comment: a bare link, nothing staged
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC, captions=()),
                    _fetch_transcript=lambda v, log: None)
    assert res.rung == "link" and res.fallback_rung is None and res.fallback_markdown is None


def test_ladder_rung_1b_ingredients_written_method_spoken():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(INGREDIENTS_ONLY_DESC),
                    _fetch_transcript=lambda v, log: _transcript(words=700))
    assert res.rung == "description+transcript", res.rung
    assert res.links == []                                   # the cookbook and the apron are not links
    assert "## Video description" in res.markdown and "Ingredients:" in res.markdown
    assert "the METHOD" in res.markdown and "Recipe found in: description+transcript" in res.markdown
    assert res.capture_extra()["rung"] == "description+transcript" and res.reject is None
    # the same description without a transcript is not a recipe: no steps anywhere
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(INGREDIENTS_ONLY_DESC, captions=()),
                    _fetch_transcript=lambda v, log: None)
    assert res.rung is None and res.reject
    # ingredients written + "full recipe on my blog" + a transcript: the blog is the recipe of record
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(INGREDIENTS_PLUS_BLOG_DESC),
                    _fetch_transcript=lambda v, log: _transcript(words=700))
    assert res.rung == "link" and res.links[0].endswith("/recipes/carne-asada")
    assert res.fallback_rung == "transcript"


def test_ladder_rung_3_comment():
    res = ys.ladder(URL, description=None, comments=["great vid!!", COMMENT_RECIPE], log=_quiet,
                    _fetch_watch=lambda v, log: _info(CHATTY_DESC, captions=()),
                    _fetch_transcript=lambda v, log: None)
    assert res.rung == "comment", res
    assert res.comments_used == 1 and "## Comment 1" in res.markdown
    assert "great vid" not in res.markdown


def test_ladder_rung_4_transcript_only_is_flagged_spoken():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(CHATTY_DESC),
                    _fetch_transcript=lambda v, log: _transcript())
    assert res.rung == "transcript"
    assert "Recipe found in: transcript" in res.markdown
    assert "the cook speaking" in res.markdown


def test_ladder_rung_5_polite_reject():
    res = ys.ladder(URL, comments=["first!"], log=_quiet, _fetch_watch=lambda v, log: _info(CHATTY_DESC, captions=()),
                    _fetch_transcript=lambda v, log: None)
    assert res.rung is None and res.markdown is None
    assert res.reject and res.reject.startswith("We read this video's description, its first comments")
    assert "no transcript" in res.reject


def test_ladder_short_transcript_is_not_a_cook_talking():
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(CHATTY_DESC),
                    _fetch_transcript=lambda v, log: _transcript(words=40))
    assert res.rung is None and res.transcript_words == 0


def test_ladder_uses_browser_description_when_watch_page_unreadable():
    res = ys.ladder(URL, description=FALLOW_DESC, log=_quiet, _fetch_watch=lambda v, log: None,
                    _fetch_transcript=lambda v, log: None)
    assert res.rung == "description" and res.title == ""


def test_markdown_result_shape_and_rejects():
    ok = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(FALLOW_DESC),
                   _fetch_transcript=lambda v, log: None)
    # a bare link (no transcript, no comment), and a link WITH a transcript —
    # both built before ys.ladder is replaced below
    bare = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC, captions=()),
                     _fetch_transcript=lambda v, log: None)
    with_fb = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(LINK_ONLY_DESC),
                        _fetch_transcript=lambda v, log: _transcript())
    saved = ys.ladder
    try:
        ys.ladder = lambda url, log=print: ok  # type: ignore[assignment]
        out = ys.markdown_result(URL, log=_quiet)
        assert set(out) >= {"markdown", "source_url", "title", "has_jsonld", "jsonld", "youtube", "hero_image_url"}
        assert out["has_jsonld"] is False and out["youtube"]["rung"] == "description"
        # a bare link (no transcript, no comment): the URL door says where to go
        ys.ladder = lambda url, log=print: bare  # type: ignore[assignment]
        try:
            ys.markdown_result(URL, log=_quiet)
            raise AssertionError("expected RuntimeError for a link-out")
        except RuntimeError as e:
            assert "example-kitchen.com" in str(e)
        # a link WITH a transcript: the URL door reads the transcript, keeps the link on the capture
        ys.ladder = lambda url, log=print: with_fb  # type: ignore[assignment]
        out = ys.markdown_result(URL, log=_quiet)
        assert "Recipe found in: transcript" in out["markdown"]
        assert out["youtube"]["rung"] == "transcript" and out["youtube"]["links"][0].endswith("/recipes/beef-pie")
    finally:
        ys.ladder = saved


def test_capture_carries_channel_identity():
    """The channels table is fed from `_source.capture` at save time, so the
    ladder's capture block must name the channel by its stable id and handle,
    not just its display name (video lane step 2)."""
    res = ys.ladder(URL, log=_quiet, _fetch_watch=lambda v, log: _info(FALLOW_DESC),
                    _fetch_transcript=lambda v, log: None)
    cap = res.capture_extra()
    assert cap["channel"] == "Fallow"
    assert cap["channel_id"] == "UCJ901NqoRaXMnIm7aOjLyuA"
    assert cap["handle"] == "FallowChefs"
    # An unreadable watch page leaves the identity ABSENT, never a blank string.
    res2 = ys.ladder(URL, description=FALLOW_DESC, log=_quiet, _fetch_watch=lambda v, log: None,
                     _fetch_transcript=lambda v, log: None)
    assert res2.capture_extra()["channel_id"] is None
    assert res2.capture_extra()["handle"] is None


def test_non_youtube_url_is_a_value_error():
    try:
        ys.ladder("https://example.com/recipe", log=_quiet)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert ys.is_youtube_url("https://youtu.be/XE6JvrChQ_Q")
    assert not ys.is_youtube_url("https://vimeo.com/123")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
