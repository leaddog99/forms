# Capture sources: recipes from Facebook, Instagram and other locked-down pages

*Written 2026-10-06. Code: `intake/capture_sources.py`, `forms/importing.html`,
the v4 loader in `forms/install.html` / `forms/bookmarklet.js`, `/stage-capture`
and `/capture-source` in `save_recipe_api.py`.*

## The problem, in two halves

More and more of the recipes people want to keep are a caption under a video,
not a page on a recipe blog. Two things are different about those pages.

**Transport.** The recipe bookmark is a one-line *loader* that injects our
payload script from our host. Facebook's Content-Security-Policy allows scripts
only from Facebook's own hosts, so the browser refuses the script and nothing
runs; the popup sat on "Loading recipe importer..." for ever. Even if the script
loaded, the same policy forbids the `fetch` that stages the page with us. And
the server cannot fetch the post instead: logged out, Facebook returns a
truncated preview. The only full copy of the text is in the person's signed-in
browser.

**Structure.** A caption has no headings and no schema.org markup. Quantities
are inline, bullets are emoji, hashtags trail. On Facebook the method is
routinely "in the first comment", and on the reel we checked the first comment
was the page author posting `Recipe >>>> https://ketosl.com/…`: the recipe was
not on Facebook at all. A good share of these grabs will come through thin.

## What was built

### A typed source, not a Facebook special case

`intake/capture_sources.py` registers each platform as a `CaptureSource`: host
suffixes, the structure (`post`, `reel`, `caption`, `thread`, `video`,
`article`), what the model should know about text of that shape, what the
person should expect, and what to say when the result is thin. Six are
registered: Facebook (verified against a live reel), Instagram, Reddit,
YouTube, Medium, Substack (the others are assumptions until each is tried, and
say so with `verified=False`).

The server is the authority on the kind. `classify_capture_url(url)` decides it
from the host; the loader never says "this is Facebook", it only captures.

### One door in, then the one path

`POST /stage-capture` takes what the bookmark's own code could read (text,
author comments, image, whether the person had selected text) and converts it
into the same staged markdown envelope every other grab becomes: a title line,
`*Source:*`, `*Captured:*`, a rule, the body. From there it is the ordinary
path with no knowledge of the door: `/staged-markdown/{token}` →
`/extract-from-markdown` → `markdown_to_recipe`. There is no second extractor.

Three things ride along as provenance and guidance:

- `_source.capture` on the recipe: `{kind, structure, via, selected, chars,
  comments, links}`. Every social-capture recipe is findable, which the
  attribution fix below needs.
- The kind's model note is appended to the extraction prompt beside the
  publisher's own `extract_notes`, decided from the URL, so a Facebook page
  gets the same guidance whichever door it came through.
- Links found in the author's comments. When the comment is only a link, the
  recipe's real address is that link, and the in-progress page offers
  "Import from ketosl.com instead", which is our normal URL path.

### The loader does the capture when the payload cannot

The v4 loader opens `forms/importing.html?url=…` and injects the payload as
before. It also arms a fallback on two triggers: the script element's `error`
event (Chrome fires it for a CSP block) and a four-second timer. The payload's
first statement sets `window.__bccPayloadStarted`, so on an ordinary page the
fallback never fires. When it does fire, it:

1. clicks any short "…more" button so a long caption is fully shown, and opens
   the comments panel if none is open;
2. after the DOM settles, takes the person's **selection** if they made one;
   else the post body (`[data-ad-preview=message]` on a Facebook post; on a
   reel, where no such mark exists, the longest `dir=auto` span); else
   `article`/`main`; else the body;
3. takes the **author's own comments**: `[role=article][aria-label^=Comment]`
   whose text carries the "Author" badge;
4. takes `og:image`, else the largest image, else the video poster;
5. navigates the popup to the in-progress page with the capture base64url-encoded
   in the URL **fragment**. A fragment is never sent to any server, and
   navigation is not governed by the page's `connect-src`.

The payload has the same fallback for the other case: a page that lets the
script run but refuses the `fetch`. It hands over the markdown it already built,
through the same fragment.

Constraints on the one-liner, because it lives inside a double-quoted string in
`install.html`: no double quotes and no backslashes. Hence attribute selectors
without quotes and `split/join` instead of regex escapes.

### The in-progress page

`forms/importing.html` is what a person sees between pressing the bookmark and
the editor opening. It owns the whole wait: sign-in in parallel with the grab,
receiving the token (fragment or the `/staged-latest` poll), staging a fallback
capture, the extraction with honest phase progress, and handing the result to
the editor through `sessionStorage` in the same tab. The editor's
`applyExtractResult` then does exactly what it did when it ran the call itself.

Its one memorable element is a recipe sheet that assembles as the pipeline
reports progress: the photo and title the moment the grab lands, ingredient
lines as the model reads, steps after, equipment at the end. A thin result from
a social source gets its own state: what we found, the platform's own advice,
"Open in the editor anyway", and the linked page when there is one.

Older v3 bookmarks keep working. The payload still sends them to the editor's
awaiting state and tells them a newer bookmark is available.

## Still to do

- **Attribution.** Our publisher record is keyed by host, so every Facebook
  recipe attributes to facebook.com. The page that posted it is the author.
  f2n's link cards already apply that rule for Facebook; recipes needs the
  same before social grabs pile up under one domain. `_source.capture` marks
  the rows to fix.
- **Instagram, Reddit, YouTube, Medium, Substack** are registered and untested.
  Try one real page each; correct the notes and the loader's selectors from
  what the DOM actually shows, as was done for the reel.
- **Instagram images.** The recipe is often in the picture, not the caption.
  That is a vision problem, not a capture problem; the existing image path is
  the place for it.
- **Selection first.** The install page now says to select the recipe text on
  Facebook, Instagram or Reddit before pressing the bookmark. It is the most
  reliable capture of all and sidesteps every selector.
- **How do they fare?** Every typed extraction logs
  `[CAPTURE] result kind=… ingredients=… steps=…`. After a few weeks, read
  those lines before changing anything.
