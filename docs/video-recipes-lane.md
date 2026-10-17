# Video recipes — a lane, not a second table

*DESIGN, 2026-10-17. Curator's prompt: "separate Recipes from YouTube Recipes (and
maybe fb/instagram/etc) and rank/score those differently… a different tab with a
different storage/table format." Informed by the Butter Chicken experiment
(`docs/youtube-ladder.md` §"Video authority") and the ReciMe dossier
(`docs/research/recime-dossier.md`). Nothing here is built.*

## 1. The decision in one paragraph

A video recipe, once the ladder has read it, is the same object as any other
recipe and lives in the same tables; what is separate is its **lane**: a source
class on the row, its own ranking method and seats inside a dish, a channel
record where the publisher record would be, and a tab that shows the class on
its own. A second table would mean a second model, a second editor and its four
save edges, a second search index, and every downstream surface (cookbooks,
menus, cook view, activity, backups, the Postgres port) learning two shapes.
The single-canonical-path rule exists because the last parallel pipeline
rotted; this design keeps it and still gives the curator the separation asked
for. The one new table that is warranted is `channels`, because the domains
table is keyed by host and a channel handle is not a host.

## 2. What the evidence says

- **Foreign-locale harvests are video.** Butter Chicken on a Hindi line: 36
  SERP results, 33 YouTube or Facebook, all dropped as blocked hosts; 3 pages
  left, Moz data for none, 2 recipes kept by reserve alone.
- **Moz cannot rank a video.** 3 of 4 watch pages uncrawled; the 4th scored
  PA 75 against DA 100, so OU is meaningless; 16 billed rows for 4 URLs.
- **SEMrush can, at the URL level, in English-indexed markets.** Fallow's
  Butter Chicken video: ~250 US visits a month from 20 dish keywords, #3 for
  "butter chicken recipe." The India database has nothing for the Devanagari
  query itself.
- **The ladder reads them.** 3 of 4 ranking videos carried the written recipe
  in the description.
- **The competitor reads less than we do.** ReciMe's ladder is caption → audio
  → linked site and never reads comments; its Chrome extension is our
  bookmarklet. Its one rung we lack is audio for Instagram, TikTok and
  Facebook, and its reviews show that rung is also where its errors come from.

## 3. The source class

A generated column on both recipe tables, indexed and faceted exactly like
`cuisine` and `chapter`:

```
source_class  TEXT GENERATED ALWAYS AS (
  CASE json_extract(data,'$._source.capture.kind')
    WHEN 'youtube'   THEN 'video'
    WHEN 'facebook'  THEN 'social'
    WHEN 'instagram' THEN 'social'
    WHEN 'tiktok'    THEN 'social'
    WHEN 'reddit'    THEN 'web'
    WHEN 'medium'    THEN 'web'
    WHEN 'substack'  THEN 'web'
    ELSE 'web' END) VIRTUAL
```

Three classes, deliberately few. **web** is a page we fetched or the browser
captured and that a publisher owns. **video** is a recipe whose source of record
is a video platform the server can read (today: YouTube). **social** is a recipe
a person saved from a network the server cannot read (Facebook, Instagram,
TikTok); it arrives only through the browser or a share door, carries its
capture stamp, and is never ranked, because there is no SERP position and no
traffic to rank it by. The capture kind is already stamped on every staged grab
and on `_source.capture` of the saved recipe; the column just makes it
cheap to filter. ALTER TABLE on these tables runs with sqlite-vec loaded, in a
one-off, never in the startup ensure (the 10-16 restart loop).

The class travels with the row through claim, cache and promote, like the
identity card does, so a video recipe promoted to a dish stays a video recipe.

## 4. The ranking lane

### 4.1 Admission at the SERP stage

youtube.com stays on the blocked-host list for the **web** lane. A new step
after the SERP collects results and before `filter_disallowed`:

1. Partition the organic results: video hosts (youtube.com, youtu.be) into the
   video lane; everything else continues as today.
2. For each video URL, run the ladder with `want_transcript=False` (the watch
   page only; the transcript costs 2 s and is fetched later for the winners).
   Admit the video when the ladder reports **rung 1** (a written recipe in the
   description). Rung 2 (a link out) hands the linked page to the web lane as
   an ordinary candidate, which is the real recipe anyway. Rung 3 is browser-only
   and does not arise at harvest. Rung 4 (transcript only) is **not admitted to
   a harvest**: a spoken recipe with no written record is a bookmarklet save, not
   a cohort member.
3. Record every video result in the acquisition ledger with its rung, admitted
   or not, so the class `video:rung4` and `video:link` are measurable before
   anyone decides to widen admission.

Facebook and Instagram results stay blocked at harvest: the server cannot read
them, and nothing in the dossier suggests a legitimate way to.

### 4.2 Rank key

Moz is not called for video URLs (saves the 4-variant probe per URL). The lane
ranks on signals we already own or can buy honestly:

| signal | source | when |
|---|---|---|
| `serp_position` | the SERP result order for the dish query, per line | free, at harvest |
| `serp_lines` | how many of the dish's search lines returned the video | free, at harvest |
| `url_traffic` | SEMrush URL-level organic traffic in the line's market | ~400 API units per URL; English-indexed markets only; stamped with method + date, absent elsewhere ([[feedback_absent_not_zero]]) |
| `channel_standing` | the channel's own record (§5): how many dishes it has ranked for, its mean position, curator flags | accrues over time |

Rank within the lane: `serp_position ASC` as the primary key, `url_traffic DESC
NULLS LAST` as the tie-break, `channel_standing` as the second tie-break; a
curator pin beats all. This is deliberately not a formula over YouTube's own
numbers: views, likes and subscribers stay out, per the Data API policy decision
and because a number read off the watch page is still YouTube's number. The
method id is stored with the rank (`rank_method = 'video_serp_v1'`), as every
derived value is.

### 4.3 Seats

The dish membership junction (`collection_members`, keyed by collection type)
gains a video lane: either a second collection type `dish_video` or a `lane`
column; the former needs no schema change and keeps every existing query
untouched, so it is the recommendation. The dish gets a `video_seats` setting
(default 3, System → Limits cap 10) that works like a search line's reserved
seats: the lane fills its own seats from its own candidates, ranked among
themselves, and never competes for the web top-N. A dish with no admitted
videos simply has an empty lane; nothing is backfilled from the web lane.

The dish refresh log prints the video lane after the web batch, in the same
shape: candidates, admitted (with rung), ranked, seated.

### 4.4 Cost

Per dish refresh: the watch-page reads (≈1 s each, no spend), SEMrush only for
seated winners in English-indexed markets (3 × 400 units), transcript fetch for
seated winners only, and the extraction call per seated winner, which is the
same cost as a web page. Rejected and transcript-only videos cost nothing but
the watch-page read.

## 5. The channel record

A `channels` table parallel to `domains`, because the publisher of a video is a
channel and the domains table is keyed by host:

```
channels(channel_id TEXT PRIMARY KEY,      -- YouTube's UC… id, stable across renames
         handle TEXT, display_name TEXT, platform TEXT DEFAULT 'youtube',
         url TEXT, country TEXT, language TEXT,
         dishes_ranked INTEGER, mean_position REAL, recipes_saved INTEGER,
         standing REAL, standing_method TEXT, standing_at TEXT,
         harvestable INTEGER DEFAULT 1, notes TEXT, known_for TEXT,
         created_at TEXT, updated_at TEXT)
```

Rows are created on first admission and the counters recomputed at each refresh
that touches the channel. A channel row is what the channel editor edits (clone
and specialise the domain editor, not a metadata-driven runtime), and it is
where a curator says "never this channel" or "always seat this one." The recipe
carries `channel_id` in `_source.capture`, which it already does by name; the
id is added by the ladder from the watch page's `channelId`.

## 6. The tab

No new page. The recipe-table-backed list component gains the `source_class`
facet, which gives the recipe library a "Video" tab and a "Social" tab for
free, with the same sorts (best match still leads under every sort). The dish
editor's cohort view shows the video lane as a second block under the winners,
with the rung and the rank signals in place of OU and PA, and the thumbnail
that every video already has. The one new control is `video_seats` beside the
existing per-line reserve. The ⓘ on the tab explains the class in one
paragraph.

## 7. The doors (the part ReciMe does better)

The lane is the harvest side. The dossier's "what to take" list is the save
side and belongs here because the social class only ever arrives through it:

- **Share-sheet door.** An iOS Shortcut and an Android share target that POST
  the URL to `/stage-capture` with `via: share`. The ladder handles YouTube; for
  Facebook and Instagram the door returns the polite reject with the
  screenshot door offered.
- **Screenshot door.** One to three images in, staged markdown out, through
  the vision-capable extractor; the only door that reads comments and on-screen
  text on networks the server cannot see. Capture kind `screenshot`, class
  `social`.
- **Rung pill.** `capture.rung` shown in the editor as provenance: from the
  description, from a comment, from the transcript, from a screenshot.
- **The audio rung, deliberately not now.** For Instagram, TikTok and Facebook
  the only version that does not have us fetching media from Meta or ByteDance
  is a speech pass over media the user's browser already holds, which the
  bookmarklet cannot reach today. Park it behind the screenshot door and
  measure how often the screenshot door is used before building it.

## 8. What is not in scope

Facebook and Instagram at harvest; any score built from view, like or
subscriber counts; a public share URL for a saved social recipe (the
contradiction in ReciMe's framing, see the dossier §5); frame OCR of video.

## 9. Build order, when approved

1. `source_class` column + facet + tab (one-off ALTER with vec loaded; the list
   component and the dish cohort view).
2. `channels` table + ladder stamping `channel_id` + the channel editor.
3. Harvest admission + the video lane in the refresh (ledger first, so the
   rung classes are counted before anything is seated).
4. Seats and the `video_seats` control; SEMrush stamping for seated winners.
5. Share-sheet and screenshot doors; rung pill.

Each step verifies against Butter Chicken on the Hindi line, where today's
answer is two recipes and 33 videos on the floor.
