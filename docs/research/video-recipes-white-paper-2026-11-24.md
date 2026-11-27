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

A later check found a real fault in one of the five: in a Beef Wellington video, the crepe quantities in the written ingredient list (1 cup of flour, 2 eggs) do not match what the cook says and does on camera (2 cups, 4 eggs). So a recovered method cannot be trusted without being checked.

### The strict reading, tried on real videos

We then ran the full five-question reading (described under "What we decided") on nine real videos. Six passed and three failed.

| Video | Verdict | Why |
|---|---|---|
| Mary Katherine's Table, sweet potato casserole | Pass | |
| The Carefree Kitchen, sweet potato casserole | Pass | |
| Laura in the Kitchen, sweet potato casserole | Pass | |
| Sip and Feast, roast leg of lamb | Pass | |
| Chef Bhupi's Kitchen (Hindi) | Pass | |
| Spice N' Pans, mapo tofu | Pass | |
| Nick DiGiovanni, beef Wellington | Fail | Written crepe quantities contradict the spoken ones |
| Amy's Louisiana Kitchen, sweet potato casserole | Fail | She cooks a doubled batch, so spoken sugar and eggs disagree with the written list; our method also added a baking time nobody stated |
| Joshua Weissman, beef Wellington | Fail | Our method says 2 tablespoons of searing oil; he says 3 |

Every failure was about quantities, and the lesson is a surprising one: in two of the three, the disagreement is between the creator's own written list and the creator's own words. A cook reading the list and a cook watching the video would make different dishes. The reading also caught two deliberately damaged recipes we fed it (one with half the method removed, one with an invented step).

Whether failing a recipe over one tablespoon of oil is the right degree of strictness is a judgement for the curator, and part of the comparison with a human viewer that is still owed.

## Popularity is not quality

The curator's experience, which the numbers support, is that the most-watched food videos are often entertainment: jokes, a tour of street-food stalls, a challenge, a meme. They can be hugely popular and useless as a recipe. Letting them in would damage the integrity of the whole library.

Views per day therefore does two limited jobs. It sets a floor (a video nobody watches is not a find), and it orders the videos that have already passed every other test. It never admits a video by itself.

## Why web measures do not carry over

We rank web recipes partly on the authority of the page and the site. Neither works for video. The authority service had data for only 2 of 26 video pages. And when we compared the views of 56 videos with the authority of their creators' own websites, there was no relationship worth using (a correlation of 0.11, where 1 is perfect and 0 is none). Some of the most-watched videos come from creators whose websites are nearly invisible, and some of the strongest recipe sites draw ordinary video audiences. Video and web are different audiences, so videos are judged on their own terms.

## Where the videos come from

The search engine matters a great deal. The same Butter Chicken search returned 111 videos on the old engine and 7 on the new one, because the new engine shows only a handful of videos per search. Fewer videos means fewer good candidates, and for some dishes none at all.

## Reading captions is a privilege, not a right

On 24 November 2026, after a day of reading captions heavily, YouTube began refusing every caption request from our machine. The video pages themselves still loaded; only the captions were withheld. This matters because the whole ingredients-only kind depends on captions.

We made three changes in response. A video's captions are now read once and kept, so the same video is never asked for twice. Requests that do go to YouTube are spaced out, about twenty seconds apart on average and at irregular intervals. And when YouTube refuses, the request is retried through the paid service we already use for web pages that block us. That paid route worked on the first attempt while the refusal was still in force. If all of that fails, a dish simply keeps the videos it already has.

We considered YouTube's official programming interface. It would give dependable facts about a video (views, date, channel) and a way to search for more videos. It would not give us captions for other people's videos, which is the part that was refused. It also comes with legal conditions on how its data may be stored and displayed. The curator has put it on hold until those conditions are read properly.

## Two standards: the library and the individual

The dish library is curated and strict. A person saving a recipe for their own use is in a different position: they may want a spoken-only recipe precisely so they can fill in the blanks themselves in the editor. Refusing them would be wrong.

So a personal save of a video recipe is never refused. When the recipe is questionable, the person sees one polite, specific warning and chooses to save or cancel. For a spoken-only video the warning says that nothing was written under the video and the recipe is our reading of the captions. For an ingredients-only video the strict reading is run, and if it finds a problem the warning says exactly what, for example that the written crepe quantities contradict the spoken ones. A recipe that passes is saved with no interruption. Once acknowledged, the warning is not shown again for that recipe.

## What we decided

1. Videos get their own small number of places per dish (three by default) and never take a web recipe's place.
2. A video with a full written recipe is eligible.
3. A video with written ingredients and a spoken method is eligible only if a strict second reading passes it. That reading asks five questions: is this a cook seriously teaching the recipe; is every written ingredient used, at the written quantity; do the cooking steps carry a time, temperature or cue that was actually said; is the method complete; was anything added that the creator never said or wrote. One "no" and the video is left out. In doubt, it is left out.
4. A video that only links to a recipe hands that page to the web side.
5. A spoken-only video is not considered, however popular. The run says so politely.
6. A video must draw at least 10 views a day. Of the first 49 eligible videos, 14 fell below 10 and the next lowest was 27, so the line falls in a natural gap.
7. These rules govern the dish library. A person saving for themselves is warned, never refused.
8. Captions are read once, kept, requested slowly, and fetched through the paid route when refused.
9. YouTube's official interface is on hold until its legal terms have been read.

## What we do not know yet

- **Whether the strict reading agrees with a person.** Nobody has yet watched a set of judged videos and compared verdicts. The reading works from captions, not from the video, so a misheard number would pass. This is the most important open test.
- **Whether the "serious lesson" question can say no.** All nine real videos were genuine lessons and it passed them all. It has not yet met a joke video, a street-food travelogue or a meme, which is the case it exists for.
- **Whether the strictness is set right.** One recipe failed over a single tablespoon of oil.
- **How long caption access will be refused,** and what the paid route costs at volume (about three paid requests per video).
- **How the personal-save warning reads on screen.** It has been tested behind the scenes, not yet by a person saving a recipe.
- **Whether three places per dish is right,** and whether popular dishes deserve more.
- **Whether one channel should be able to take every place.** Places go to the most-watched videos, so a prolific channel can fill all three. In the first Shrimp Stir Fry run, five of the eight eligible videos came from a single channel and were also the most watched. A rule of one place per channel is noted as a to-do and deliberately not built until we have seen more dishes. That same run then gave all three places to the one channel, at 4,601, 4,288 and 2,013 views a day; a one-per-channel rule would have replaced two of them with videos at 64 and 28. Seeing that, the curator is leaning towards leaving the rule out: three heavily watched videos are worth more than two fill-ins hardly anyone watches.
- **How the sample holds up.** 152 videos across 23 dishes is enough to see the shape, not to trust every percentage.

## Change log

- **24 November 2026.** First version. Covers the four kinds of video, the caption test, the comparison with site authority, the search-engine difference, and the six decisions.
- **27 November 2026.** Added the open question of one channel taking every place (to-do, deferred).
- **24 November 2026, later.** Added the strict reading's results on nine real videos (six pass, three fail, all on quantities); YouTube's refusal of caption requests and the three responses; the official interface put on hold; the separate, lenient standard for personal saves; decisions 7 to 9.
