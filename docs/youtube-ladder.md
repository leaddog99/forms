# YouTube as a recipe source — the ladder

*2026-10-17. Code: `intake/youtube_source.py`, `intake/capture_sources.py`
(the `youtube` kind), the three doors in `save_recipe_api.py`, the YouTube
branch of `forms/bookmarklet.js`, the v5 loader in `forms/install.html`.*

## What a recipe video is

A cooking video on YouTube carries its recipe in one of a few places, and the
places are not equal. Many creators paste the complete recipe into the
description (Fallow's "Michelin Beef Pie at Home": 5,000 characters of
ingredients and method under the video). Others put a link to their blog
there, and the blog is the recipe. Some channels put the recipe in a pinned
comment. And every video with speech has an auto-generated caption track,
which is a cook talking — "a knob of butter", "a couple of onions" — not a
written recipe, but often enough to cook from.

So we read those places in a fixed order and stop at the first that holds a
recipe:

| rung | source | who can read it | what it is |
|---|---|---|---|
| 1 | description | server (watch page) or browser | the recipe of record when it has one |
| 2 | a link in the description | server or browser | a written recipe elsewhere — our ordinary URL path, offered instead |
| 3 | the first comments | **browser only** (YouTube loads comments on scroll) | the creator's own comment, sometimes the recipe |
| 4 | the transcript | server (our address) | the cook speaking; quantities stay as spoken |
| 5 | polite reject | — | nothing above holds a recipe; one sentence says so, nothing is spent |

Whichever rung supplies the recipe, the transcript rides along in the staged
text as corroboration. The extractor sees what was actually said and checks
sequence, times and temperatures against it, and fills a step the description
skips but the cook performs. That is the audit, done at the one point where it
can still change the result. A spoken approximation never overrules a written
quantity; when the transcript is the only source the text says so
("Recipe found in: transcript") and the model is told to keep the cook's
words rather than invent numbers.

"Does this text hold a recipe?" is `recipe_signal`: ingredient-looking lines
(a quantity and a unit or a countable food) and step-looking lines (numbered,
or sentences under a Method heading). Conservative on purpose: a description
that says "full recipe on my blog" must not pass, or the ladder stops on the
wrong rung and the written recipe behind the link is never offered.

## No YouTube Data API — decision 2026-10-16

The curator asked whether the Data API could read transcripts and supply
stats to score recipe videos. Researched and decided against ("option 2"):

- `captions.download` requires an OAuth scope and *permission to edit the
  video* — it only works on a channel we own. The API never returns another
  channel's transcript.
- The stats are there (views, likes, comments, duration, subscriber count,
  1 quota unit a call), but the YouTube API Services Developer Policies
  §III.E.4.h forbid an API client from using them to create *derived data or
  metrics* — the example given is a score that factors in likes and views —
  require counts to be shown verbatim with YouTube branding, cap storage of
  unauthorized statistics at 30 days (§III.E.4.b), and bar an API client from
  fetching YouTube data by any other technology (§III.I.14) or from scraping
  (§III.E.6). Holding a key would have forbidden exactly the scoring asked for
  and would have put the transcript library out of bounds.

Everything here therefore comes from the public watch page
(`ytInitialPlayerResponse`: title, channel, description, length, which
caption tracks exist) and from the caption endpoint the page itself uses,
read with `youtube-transcript-api`. No key, no API terms. The transcript
fetch works from a residential or business address and is refused from cloud
ranges (empty bodies, IP blocks) — a property of where BCC runs, not of the
code; if BCC ever moves to a cloud host, rung 4 needs a managed transcript
service (Supadata, TranscriptAPI and the like, roughly a cent a video) or a
residential proxy. Video *authority* for ranking, if ever wanted, is the
signal we already own: whether the SERP sends the video for the dish query,
and the channel treated like a publisher domain — never a formula over
YouTube's numbers.

## The doors

All three end in the ONE staged-markdown path
(`/staged-markdown/{token}` → `/extract-from-markdown` → `markdown_to_recipe`);
nothing downstream knows the text came from a video except through the
`capture` block.

1. **Bookmarklet on a watch page** (`forms/bookmarklet.js`, YouTube branch).
   The payload does not convert the DOM — player chrome is not a recipe. It
   expands the description, scrolls the comments into view, reads the first
   three threads (tagging `[pinned]` / `[creator]` when YouTube shows those
   badges), takes `og:image`, and hands the capture to `forms/importing.html`
   by URL fragment — the same door as the CSP fallback. The importing page
   posts it to `/stage-capture`, which climbs the ladder: server read of the
   description (cleaner than the DOM's), transcript, decision. The staged
   `capture` block carries `rung`, `channel`, `channel_id`, `handle`, `length_s`, `transcript_words`,
   `caption_language`, `links`, and `reject` when rung 5 (or a link-out) is
   the answer. The importing page shows a `reject` as a plain sentence with
   the link, when there is one, as the primary action — before any extraction
   runs, so a reject costs nothing.
2. **The payload's ordinary door** (`/stage-markdown`) when a YouTube URL
   arrives there: the DOM markdown is replaced by the ladder's text. Belt
   and braces for an older payload build.
3. **A pasted URL** (`extract_recipe_from_url`, the form's URL box, batch):
   `youtube_source.markdown_result` stands in for `html_to_markdown` with the
   same return shape, plus `og_meta` (thumbnail, "YouTube", channel) so the
   existing hero coopt and site-name stamp work unchanged. A link-out or a
   reject raises with the user-facing sentence, which the form shows.

The thumbnail is derived from the video id (`img.youtube.com/vi/<id>/
maxresdefault.jpg`, the creator-sanctioned still) and cooped into our store
like any other hero.

## The loader fix (v5)

The hang the curator saw on a watch page was Trusted Types. YouTube enforces
`require-trusted-types-for 'script'`, under which `script.src = "<string>"`
throws a `TypeError`. The v4 loader did that assignment *before* it wired its
`onerror` and its 4-second fallback timer, so the exception killed the loader
with the popup already open and nothing ever arriving in it. v5 wraps the URL
in a Trusted Types policy (`trustedTypes.createPolicy('bcc<ts>', …)`, which
YouTube allows — verified 2026-10-17) and, when a page refuses even that,
runs the DOM-capture fallback at once. `LOADER_V` in `bookmarklet.js` is 5;
an installed v4 bookmark keeps working everywhere it did and is told once to
re-install. Nothing else in the one-liner changed.

## Verified

- Fallow, "Michelin Beef Pie at Home" (`XE6JvrChQ_Q`), 2026-10-17: watch
  page read in under 2 s; description 4,987 chars, 19 ingredient lines,
  49 step lines → rung 1; transcript 4,672 words (English, auto-generated);
  staged text 29 k chars. The transcript names Worcestershire, beurre manié,
  suet and cartouche *zero* times while the description lists all four —
  the description is the record, the transcript the commentary.
- Fallow, "The Ultimate Butter Chicken" (`8V6Krk8DDuc`), 2026-10-17, the
  first live bookmarklet press: two defects. `/stage-capture` passed `links`
  twice (the capped list and the ladder's copy) → 500 on every watch page
  while the pasted-URL door worked. And the description's headings were
  starred ("*Step by Step*"), which the method-heading regex did not know, so
  a description holding the whole recipe (29 ingredient lines, 14 steps) fell
  to the transcript rung. Both fixed; the description is rung 1 again.
- Unit tests: `python -m intake.tests.test_youtube_source` (no network).

## Video authority — the experiment (2026-10-17)

The Butter Chicken refresh on a Hindi search line (gl=in hl=hi) returned 36
organic results of which **33 were YouTube or Facebook** and were dropped at the
SERP stage as blocked hosts; of the 3 pages left, Moz had data for none, and the
dish kept 2 recipes by reserve alone. The curator asked whether the videos could
be used, and what Moz or SEMrush would say about them. Measured:

| instrument | result |
|---|---|
| **Moz** on 4 ranking watch pages | 3 of 4 *uncrawled* (no data); the one with data (Get Curried, a decade-old video) came back PA 75 / DA 100 / OU 26 — OU against a DA-100 baseline is not a measurement. 16 rows billed for 4 URLs: canonical-variant learning cannot learn youtube.com, so every URL costs the full 4-variant probe. **Wrong instrument.** |
| **SEMrush, URL-level organic** (`resource_organic` on the watch URL) | Works. Fallow's Butter Chicken: ~250 US visits/month from 20 butter-chicken keywords (#3 for "butter chicken recipe", 110k volume); Get Curried's: ~270 India visits/month, with SEMrush noise keywords to filter ("restaurants near me open now"). ~400 API units per URL. |
| **SEMrush, keyword-level** for the Hindi-script query | *Nothing found* in the India database — SEMrush's `in` tracks English queries; the Devanagari web is invisible to it. The Hindi SERP itself has to come from Scale SERP, as it does. |
| **The ladder** on the same 4 | 3 of 4 hold the written recipe in the description (rung 1); 1 is transcript-only. |
| **SERP position** | Fallow's video is #3 in the US and #10 in India for "butter chicken recipe"; this signal is already in hand at harvest time, free. |

What this says: for a foreign-locale line the native recipe web **is** YouTube
and the blocked-host rule throws the cohort away; the videos can be read (the
ladder), and they can be ranked, but not by Moz. The signals we own are the
SERP position for the dish query (free, already captured), SEMrush URL-level
traffic where the market is English-indexed, and the channel treated like a
publisher domain. Design sketch, **not built**: admit youtube.com at the SERP
stage when the ladder reports rung 1 or 3 (a written recipe; transcript-only
stays out of harvests), carry `serp_position` as the rank key in place of the
missing Moz pair (today a Moz-less URL is dropped, not ranked), and give the
channel a row in `domains` at the `youtube.com/@handle` grain so the channel's
own standing accrues the way a publisher's does. Subscriber and view counts stay
out of any score (the Data API policy decision above; the watch page shows them
but a number read off a page is still YouTube's number).

## Not done

- Comments on the server path (a pasted URL has no browser). Rung 3 is
  browser-only by design; a server-side comment reader would be another
  InnerTube scrape and is not worth it until a real video needs it.
- A separate second-pass auditor that reports "3 quantities adjusted, 2 not
  stated in the video". The in-context check above is the audit; if the
  extracted recipes from transcript-only videos turn out to invent numbers,
  that pass is the next step, on the cook-auditor pattern.
- Shorts: classified (`structure=short`) and read the same way; untested.
