# Video recipes: what we have learned

**First written 24 November 2026. A living document: new findings are added at the end of each section and listed in the change log at the bottom.**

This paper is for people, not programmers. It records what we found when we looked hard at cooking videos as a source of recipes, what we decided, and what we still do not know. The engineering detail lives in `docs/video-recipes-lane.md`.

## The question

A search for a dish returns videos as well as web pages. Some of those videos are watched by enormous numbers of people. Should they be in a library whose promise is the best recipes for each dish, and if so, which ones, and how do we rank them?

## The short answer

Some videos belong, most do not, and popularity alone cannot tell them apart. A video earns a place only when its creator wrote the ingredients down, when the method can be recovered faithfully, and when the video is a serious lesson in making the dish. Videos are ranked among themselves by how many people watch them each day, never against web pages, because the measures we use for web pages do not work on video.

## What a cooking video actually contains

We read the page under 152 videos found across 23 dishes. They sort into four kinds, by what the creator wrote beneath the video.

| Kind | What is written under the video | Share | Typical views a day |
|---|---|---|---|
| Written recipe | Ingredients and method, in full | 19% | 185 |
| Ingredients only | The ingredient list; the method is spoken | 19% | 325 |
| Link | A pointer to the recipe on the creator's own site | 34% | 195 |
| Spoken only | Nothing; the recipe exists only as speech | 28% | 4 |

Three things stand out. First, only one video in five carries a complete written recipe. Second, a third of all videos are really advertisements for a web page, and in about seven cases in ten that link goes straight to the recipe, so the right response is to follow the link and judge the page like any other. Third, the spoken-only videos are barely watched: half of them draw four views a day or fewer.

Creators are consistent. Of the channels we met more than once, most always publish the same kind: Andy Cooks always writes the full recipe, Jamie Oliver and Allrecipes always link out.

## Can the method be recovered from speech?

For the ingredients-only kind, the method has to come from the video's captions. We tested this on five videos in three languages, using the same recipe reader the library uses for web pages. All five produced a complete, ordered method with quantities, times and temperatures, and the Hindi and French captions came out as clean English. Between 90 and 100 percent of the written ingredients were used in the steps.

That is better than expected, with three cautions. The method is our wording of what was said, not the creator's text. Captions are machine-made and mishear words (one turned "leg of lamb" into "Lego lamb"). And one of the five, found under Butter Chicken, turned out to be a mushroom dish: the search was wrong, not the reading.

A later check found a real fault in one of the five: in a Beef Wellington video, our written method gave different crepe quantities from the creator's own ingredient list. So a recovered method cannot be trusted without being checked.

## Popularity is not quality

The curator's experience, which the numbers support, is that the most-watched food videos are often entertainment: jokes, a tour of street-food stalls, a challenge, a meme. They can be hugely popular and useless as a recipe. Letting them in would damage the integrity of the whole library.

Views per day therefore does two limited jobs. It sets a floor (a video nobody watches is not a find), and it orders the videos that have already passed every other test. It never admits a video by itself.

## Why web measures do not carry over

We rank web recipes partly on the authority of the page and the site. Neither works for video. The authority service had data for only 2 of 26 video pages. And when we compared the views of 56 videos with the authority of their creators' own websites, there was no relationship worth using (a correlation of 0.11, where 1 is perfect and 0 is none). Some of the most-watched videos come from creators whose websites are nearly invisible, and some of the strongest recipe sites draw ordinary video audiences. Video and web are different audiences, so videos are judged on their own terms.

## Where the videos come from

The search engine matters a great deal. The same Butter Chicken search returned 111 videos on the old engine and 7 on the new one, because the new engine shows only a handful of videos per search. Fewer videos means fewer good candidates, and for some dishes none at all.

## What we decided

1. Videos get their own small number of places per dish (three by default) and never take a web recipe's place.
2. A video with a full written recipe is eligible.
3. A video with written ingredients and a spoken method is eligible only if a strict second reading passes it. That reading asks five questions: is this a cook seriously teaching the recipe; is every written ingredient used, at the written quantity; do the cooking steps carry a time, temperature or cue that was actually said; is the method complete; was anything added that the creator never said or wrote. One "no" and the video is left out. In doubt, it is left out.
4. A video that only links to a recipe hands that page to the web side.
5. A spoken-only video is not considered, however popular. The run says so politely.
6. A video must draw at least 10 views a day. Of the first 49 eligible videos, 14 fell below 10 and the next lowest was 27, so the line falls in a natural gap.

## What we do not know yet

- **Whether the strict reading agrees with a person.** Nobody has yet watched a set of judged videos and compared verdicts. The reading works from captions, not from the video, so a misheard number would pass. This is the most important open test.
- **Whether the "serious lesson" question works.** It has been tested only in a dry run. On the day it was written, YouTube began refusing caption requests from our machine after a day of heavy reading.
- **How reliable caption access is.** That refusal shows it can be withdrawn. When it is, a dish keeps the videos it already has.
- **Whether three places per dish is right,** and whether popular dishes deserve more.
- **How the sample holds up.** 152 videos across 23 dishes is enough to see the shape, not to trust every percentage.

## Change log

- **24 November 2026.** First version. Covers the four kinds of video, the caption test, the comparison with site authority, the search-engine difference, and the six decisions.
