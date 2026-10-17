# ReciMe — competitor dossier

*Research date 2026-10-17. Three parallel desk-research passes (technology, company and business model, legal positioning and competitive field), synthesised here. Legend on every claim: **[V]** verified from a primary or named source · **[E]** third-party estimate · **[I]** inference. Sources are listed at the end; inline references are by number. Written for the curator; the "What to take" section is the actionable part.*

---

## 1. Executive summary

ReciMe is a Melbourne-founded, New-York-based recipe organiser whose whole proposition is one verb: *save this recipe from the video you are watching*. It is the closest thing to a direct competitor to the BCC side of our system, and it is winning its category on distribution rather than extraction quality.

- **Company.** ReciMe Pty Ltd, founded 2020 by sisters Christine and Ivy Nguyen with Will Kent, all ex-consulting; app launched November 2021; founders moved to New York in April 2024 because the US was already three-quarters of users and revenue. About A$2M raised across a 2022 pre-seed and a 2024 seed led by Even Capital, with Marissa Mayer among the angels. Six people in mid-2024; hiring today is creator-marketing, not engineering. [V] [1–6]
- **Money.** Subscription only. Free tier of five "smart imports" a week; ReciMe Plus at roughly $30–60 a year depending on test cell and region; gift cards. No affiliate, no grocery commerce partner, no ads disclosed. Revenue estimates sit around US$2M a year; the company said A$60k ARR in late 2023 and twenty-fold growth by mid-2024. [V/E] [7–12]
- **Traction.** 4.8 stars on 314k US App Store ratings, #11 Top Free Food & Drink the day of research; 1M+ Android installs and a Google Play "Best of 2023" award; "15 million cooks" claimed on the homepage, almost certainly cumulative downloads. Chrome extension ~30k users. [V/E] [13–17]
- **Technology.** A server-side import ladder, publicly documented: caption or description first, then the video's audio, then the original website linked from the post. Comments are never read. No on-screen text recognition. The Chrome extension is architecturally our bookmarklet: it ships the browser-rendered HTML to their API and the server extracts. No speech-to-text or model vendor is named anywhere. [V] [18–24]
- **Positioning.** A single help-center article carries the legal cover: ReciMe is "much like a digital notebook," saves are "manual and individual," "cannot be saved in bulk," there are "no bots," saves are "automatically marked as Private," and "we do not offer any 'sharing' functionality." The last claim is contradicted by their own share-by-URL feature and a creator "featured" surface. The argument defends conduct, never subject matter. [V] [25–30]
- **Soft spots.** Import quality (missing steps, oversimplified method, wrong quantities from audio), no comment reading, no export, no offline, billing complaints. Competitors already market against the exact gap of on-screen text. [V/R] [31–34]

**Verdict:** copy their *doors* and their *framing*; do not copy their extraction, which is behind ours on YouTube already and only ahead on the one rung we lack, audio for Instagram, TikTok and Facebook.

---

## 2. The company

| fact | detail | source |
|---|---|---|
| Entity | ReciMe Pty Ltd, ABN 50 644 943 957, Suite 12/3 Albert Coates Ln, Melbourne; offices also in New York | [1][25] |
| Founded | 2020, out of a Melbourne lockdown cooking community; iOS launch 12 Nov 2021 | [2][3] |
| Founders | Christine Nguyen (CEO), Ivy Nguyen (growth), Will Kent; CTO Nic Pacholski joined early 2023 (first engineer at Sweat) | [2][4][5] |
| Funding | Pre-seed A$500k (Oct 2022: LaunchVic Alice Anderson Fund, Even Capital, Jodie Imam); Seed A$1.5M (Jul 2024, Even Capital lead; angels Marissa Mayer, Deb Liu, Karl von Randow, Paul Greenberg). No later round found. | [2][6] |
| Team | Six in Jul 2024 [V]; estimator sites say 9 to 34 today [E]. Open roles: creator marketing associate, social video producers, paid UGC creators (€50 a trial reel in Germany). No engineering postings found. | [6][35][36] |
| Location shift | US was >75% of users and revenue by 2024; founders relocated to NYC April 2024 | [37] |

## 3. Business model and traction

**Pricing.** Free tier: five smart imports a week; grocery list, meal plan and cookbooks stay free. ReciMe Plus: ten identically named App Store SKUs at $29.99, $39.99 and $59.99 in the US, which reads as paywall A/B testing plus legacy prices [I]; the help center quotes $39.99 a year (Aug 2026); third-party reviews report a rise toward $59.99 in many regions during 2026; monthly around $9.99. Lifetime SKUs existed in 2023–24 and are gone. Gift cards at $39.99 a year. Some billing runs through Stripe outside Apple's purchase system. [V/E] [7–12][38]

**Revenue.** Subscriptions and gift cards only, on all public evidence. Grocery lists are aisle-sorted and now carry an in-app "order groceries" door (reviews name Instacart and Kroger), but no partner or commission is disclosed. The 2022 vision of creators selling premium content never shipped; creators are paid for acquisition instead. [V/I] [9][20][39]

**Traction, with provenance.**

| metric | value | kind |
|---|---|---|
| US App Store | 4.8 stars, 314k ratings, #11 Top Free Food & Drink (17 Oct 2026) | [V] [13] |
| Google Play | 1M+ installs, 4.7 stars, ~110k reviews; "Best of 2023 — Best Everyday Essential" (AU, GB, HK) | [V] [14][15] |
| Chrome extension | ~30k users, v1.4.0, Feb 2026 | [V] [16] |
| Users claimed | 15k (Oct 2022) → 400k (2023) → 800k (Jul 2024) → "10 million" (Feb 2026) → "15m+ cooks" (homepage) | [V as claims] [2][6][17][40] |
| Revenue | A$60k ARR late 2023; "twenty-fold" growth by Jul 2024; third-party estimates US$1.8–2.05M/yr | [V]/[E] [6][11][12] |
| Web | ~360k visits per quarter, 80% US; US organic ~12k/mo, almost all branded | [E] [41] |

**Go-to-market.** Creator-led and paid-social-led, not SEO. An Ambassador Program pays commission on subscriptions to anyone, no follower minimum, "1,000+" ambassadors; a Recipe Creator Program offers a verified profile, in-app featured placement and link-in-bio commission. About 290 Meta ads were active at research time, with roughly 160 new creatives a week, the hook being "No more screenshotting recipes." Sponsored YouTube videos run about $775–1,500 each. The blog is not a traffic engine; organic arrivals are branded searches and help-center pricing pages. Early promotion included free-lifetime giveaways on OzBargain, posted by a co-founder to harvest reviews. [V/E] [39][42–45]

## 4. Product and technology

**The doors.** The phone share sheet is the primary door for every network: Instagram, TikTok, Facebook (reels and public groups and pages), YouTube, Pinterest. Fallbacks are pasting text, sharing screenshots or photos (up to three, including handwritten and printed pages), a Chrome extension, a desktop web app in beta, and typing. "Import from Paprika, Notes, Notion, Evernote" appears to mean paste. [V] [18–23]

**What the server reads.** The documented cascade, which they call Smart Import:

1. the caption or description;
2. the video's audio;
3. the original recipe website linked from the post, imported directly.

For YouTube the newest help article puts the website *before* the audio. Pinterest skips the pin entirely and goes to the linked site. **Comments are never read**, by their own troubleshooting page: screenshot the caption plus comments instead. Private accounts, login walls and subscription sites fail: "ReciMe can only see what's public." One recipe per post. No frame or on-screen text recognition is claimed anywhere; one reviewer says it once worked. [V] [18][24][31]

**Where the work happens.** The share sheet hands over a URL, not the post, so the caption and audio rungs run on ReciMe's side against the networks, not in the user's browser. Neither Instagram's embed endpoint nor YouTube's public API licenses that use, so those rungs almost certainly run on unofficial fetching [I] [46–48]. One clue points the other way for the audio rung: reviews complain the app "takes over my audio" during an import and praise a fix that mutes the video, which suggests the phone itself plays the video and transcribes on-device [I] [32]. The Chrome extension is the exception and is architecturally ours: a content script on every page returns the rendered page's full HTML plus a crude "contains Recipe/Ingredients/Instructions" flag; the popup posts the HTML and URL to their API; the server extracts and replies with which method succeeded and with quota headers that gate the free tier. [V, from the extension bundle] [16][24]

**Stack signals.** Marketing site on Next.js with Firebase, Google Tag Manager, Intercom, Singular attribution and a Meta pixel; help center on Intercom; billing portal with Stripe-shaped paths; extension popup in React and Vite. The privacy policy names only Google Analytics and GDPR representatives and never mentions AI, audio, transcription or a model vendor. [V] [24][49][50]

**Features beyond saving.** Grocery list by aisle or by recipe, in-app grocery ordering, weekly meal plan, an AI "Magic Meal Plan" shipped October 2026, nutrition on the paid tier, collaborative cookbooks, scaling, unit and temperature conversion, cook mode, share by message or public URL, print to PDF, a "Chef" chat feature, link back to the source as a snapshot ("a copy of the recipe at the time you saved it"). Missing: export, offline storage, Paprika file import. Release cadence roughly weekly. [V] [13][14][19][20][51][52]

**Reliability, from ~300 App Store reviews across three storefronts.** Praise centres on the audio path: "pull exact measurements from a video, writing out the spoken instructions." Complaints centre on the same path and on the method: missing or oversimplified steps, wrong quantities, "tomatoes" for "potatoes," only the first step captured from an Instagram video, ads mistaken for ingredients, every ingredient duplicated, invented titles. A month-long independent test put video-only imports around ninety percent; an AI aggregate of reviews puts "inaccurate import" complaints near ten percent. [R/E] [31–34][53]

## 5. Legal and public positioning

The framing lives in one help-center article, "Recipe Saving on ReciMe and Copyright" (updated Aug 2026), which sets out to explain "why using ReciMe to save and organize recipes does not constitute copyright infringement by ReciMe." Four moves [V] [26]:

- **Personal organiser:** "a tool for personal recipe collection and organization. It functions much like a digital notebook or folder," saving "manually and individually, just like they might copy and paste online text into a notes app."
- **No scraping, no bulk, no editorial hand:** "Recipes cannot be saved by users in bulk." "ReciMe does not use bots or automated tools to scrape or collect recipes from other websites." "ReciMe does not choose or recommend what recipes a user may wish to collect."
- **Clipper analogy:** "very similar to… Microsoft OneNote, Google Keep, Evernote, Instapaper, or Pocket" and to "Paprika, Recipe Keeper, and Copy Me That."
- **Private, not republished:** saves are "automatically marked as Private," there is no "central library," and "We do not offer any 'sharing' functionality within ReciMe… treated as personal notes as intended, not as publicly republished content."

The article never takes a position on facts versus expression, that ingredient lists are not copyrightable while instruction prose can be. It argues conduct, not subject matter.

**The contradiction.** The same help center documents "Share via URL," opened in a browser by anyone with no account, and the App Store copy sells sharing by email, SMS, WhatsApp, Messenger and AirDrop. The creator page promises recipes "featured to millions of home cooks" on "a verified creator profile." Saves are private by default and there is no community feed, but any save can be emitted as a public web page, which is one-to-one redistribution, not a note. [V] [27][28][39]

**Terms.** Everything is User Content: the user "is solely responsible," promises not to infringe, and grants ReciMe an "irrevocable, nonexclusive, royalty-free… worldwide license… to use and exploit" it. The DMCA page names a Melbourne agent and nine notice elements, has no counter-notice or repeat-infringer clause, and defines "Content" as content provided by ReciMe, so on its face does not cover user saves. Governing law is Victoria, Australia. [V] [25][29]

**Disputes.** None found naming ReciMe. The precedents in this lane are Recipeasly (2021, shut within days under blogger pressure), The Frying Pan, and the ZOE scraping row; bloggers' current anger is aimed at Google AI Overviews, not saver apps. Stashcook has copied ReciMe's framing nearly clause for clause, which suggests the wording is becoming category boilerplate. [V] [54–58]

**Platform exposure.** TikTok's terms bar automated extraction without written approval; YouTube's bar automated access except for public search engines; Meta's embed endpoint is for embedding only and forbids "extracting, or persisting" content. The "no bots" sentence is true of recipe *discovery* (user-initiated, one URL at a time) and not of the per-URL fetch mechanics. [V/I] [46–48]

## 6. The competitive field, condensed

Full table in the research pass; the shape is what matters.

| tier | who | reads from a video |
|---|---|---|
| caption only | Pestle, Plan to Eat, Copy Me That, Recipe Keeper, CookBook | the caption, or a linked page |
| caption + audio | **ReciMe**, Honeydew, JustTheRecipe, Flavorish, Mealie (self-hosted) | caption, then a speech transcript |
| caption + audio + frames | Deglaze | caption, full transcript, per-frame on-screen text, scene understanding, videos under 10 minutes |
| no social import | Paprika (v4 in beta), Crouton, Mela, AnyList, Saffron, Tandoor | web pages, photos, scans |
| publishing platforms | Samsung Food (240k public recipes, communities), Copy Me That community, Pepper | the opposite of the notes framing |

Where the field is going: the contest has moved from "does it do TikTok" to how many signals of the video are read; every new entrant gates social import behind a $35–60 a year subscription with a five-a-week free cap, including the legacy publisher MyRecipes; one-time-purchase incumbents are drifting to subscriptions; the "digital notebook plus DMCA pointer" paragraph is boilerplate; creator monetisation converges on commissions, not traffic back, because the saved snapshot deliberately removes the need to return to the source. [V/I] [59–64]

---

## 7. What to take

The curator's rule: take the best of what the competition does. Each item names where it lands in our system and what it costs.

1. **The share-sheet door.** Their primary door is the phone's share sheet; ours is a bookmarklet, which on iOS means Safari only and a fiddly install. An iOS Shortcut (and an Android share target) that receives the URL and POSTs it to `/stage-capture`, exactly as the importing page does, gives us the same door with a one-tap install page. The ladder already does the rest for YouTube. *Small build; the largest usability gap we have against them on a phone.*
2. **Screenshot import.** "Share one to three screenshots" covers the case their own ladder cannot: comments, on-screen text, private posts. We have a vision-capable extractor already; a screenshots-in, staged-markdown-out door is a thin wrapper and would make us the only one that reads comments two ways. *Small build.*
3. **The server names the method.** Their API returns which extraction method succeeded and the client shows it. Our `capture.rung` is the same fact; surface it in the editor as a one-word provenance pill ("from the description", "from the transcript", "from a comment"). *Trivial; and it is the honest thing their reviews ask for.*
4. **Quota as the paywall.** Five smart imports a week, enforced by response headers, is the whole monetisation mechanism of the category. Our own pricing is not decided, but the enforcement point is the same place: the staged-markdown door. Design the counter in when the door is built, not after. *Design note, not a build.*
5. **The copyright explainer, improved.** Their article is good because it is plain and it exists. Ours should exist too, on the BCC side, and be better in the one way theirs is weak: say what the law actually protects (expression, not the ingredient list), say what we do with a saved recipe (a personal copy, linked to its source, never republished from the user's library), and say what the TBOTB side does instead (index and judge, link out, never host the original text). Our discover-versus-possess split is a cleaner story than theirs because we do not have their contradiction: the user library is private and the published corpus is our own editorial rework. *Writing, one page; the posture is already in docs/user-as-publisher-rights.md.*
6. **Snapshot semantics, stated.** "A copy at the time you saved it, not a live link" is a user promise worth making explicitly. We already keep the staged text and the source URL; we have never told the user which they are looking at. *Copy on the editor, one sentence.*
7. **Aisle-sorted grocery list and a weekly plan.** We have menus and a merged shopping list with a parse cache; theirs is the polished consumer version and is free, which is how they get the install before the paywall. The gap is presentation, not data. *Medium; competes with the mega design pass.*
8. **Creator program mechanics, not the economics.** Verified creator profile, link-in-bio, commission on referred subscriptions. Our chef collections and the planned chefs table are the natural home for a verified-creator surface; the commission mechanics are the clickstream ledger we already have. *Later; needs a product to sell first.*
9. **Their failure list as our test set.** Missing steps at numbered-step breaks, multi-recipe pages jumbled, duplicated ingredients, ads mistaken for ingredients, only the first step captured from a short video, "tomatoes" for "potatoes" from audio. Each is a regression case for our extractor and for the transcript corroboration rule. *Free; add to the ladder tests.*

## 8. What not to copy, and where we are already ahead

- **Comments.** They cannot read them; our bookmarklet reads the creator's first comments and tags pinned and creator badges. On Facebook, where the method lives in a comment, that is the whole recipe.
- **YouTube.** Their ladder is description, website, audio. Ours is description, link, comments, transcript, with the transcript riding along as corroboration under every rung. Their reviews complain of missed steps from descriptions that plainly held them; our detector was just taught the starred-heading shape that would have caused exactly that.
- **Audio as the record.** Their praise and their complaints are the same feature. Keep spoken quantities as corroboration and never let an approximation overrule a written number, which is already our rule.
- **Snapshot without rework.** Their stored object is the creator's text, which is why their framing has to lean on "private." Our published object is a substantial rework, which is why ours does not.
- **The contradiction.** Do not offer public share URLs from a library we describe as private notes. If a user shares, share our rework or a link to the source.

## 9. The one real gap: audio for Instagram, TikTok and Facebook

For YouTube the caption track gives us the spoken recipe without touching media. For the other three networks there is no caption track; ReciMe's audio rung is a speech-to-text pass over the video, and the "takes over my audio" complaints suggest it may run on the phone. We have no equivalent, and our capture notes for Instagram say so: "the recipe itself is often on the creator's blog." Options, in order of cost: the screenshot door (item 2 above, covers on-screen text and comments and costs nothing new); a share-sheet door that lets the phone hand us the media it already has; a server-side speech pass over media the user's browser already fetched, which is the only version that does not have us fetching from Meta or ByteDance. This belongs in the video-lane design doc, not here.

---

## Sources

1. https://recime.app/privacy-policy/
2. https://www.startupdaily.net/?p=106768
3. https://screensdesign.com/apps/recime-recipes-meal-planner/
4. https://www.cbinsights.com/company/recime-1/people
5. https://www.smartcompany.com.au/startupsmart/recime-sizzles-jumping-20000-400000-users-in-2023/
6. https://www.smartcompany.com.au/startupsmart/aussie-cooking-app-startup-recime-raises-1-5-million-seed/
7. https://apps.apple.com/gb/app/recime-recipes-meal-planner/id1593779280
8. https://www.recime.app/
9. https://recime.app/help/en/articles/11630592-how-much-does-the-recime-subscription-cost
10. https://www.recime.app/gift-cards
11. https://prospeo.io/c/recime-revenue
12. https://yumishare.com/blog/recime-app-cost-in-2026-is-the-premium-subscription-worth-your-money/
13. https://apps.apple.com/us/app/recime-recipes-meal-planner/id1593779280
14. https://play.google.com/store/apps/details?id=com.recime.app
15. https://blog.google/intl/en-au/products/android-chrome-more/best-of-play-2023/
16. https://chromewebstore.google.com/detail/recime-extension/pfldidmekndhkehcoikjgaiajcfnmfga
17. https://www.recime.app/blog/news/recime-unveils-bold-new-look
18. https://recime.app/help/en/articles/11659005-import-from-instagram
19. https://recime.app/help/en/articles/11594896-what-is-recime
20. https://recime.app/help/en/articles/11596272-how-can-i-get-the-most-out-of-recime
21. https://recime.app/help/en/articles/12250372-import-from-screenshots
22. https://recime.app/help/en/articles/11625295-import-recipes-from-your-notes
23. https://recime.app/help/en/articles/11626084-can-i-access-recime-using-a-computer
24. Extension bundle, fetched from the Chrome Web Store update endpoint for id pfldidmekndhkehcoikjgaiajcfnmfga (manifest v3; content script on all URLs; POST to api.recime.app/v1/extension/importing)
25. https://www.recime.app/terms-and-conditions
26. https://recime.app/help/en/articles/11596213-recipe-saving-on-recime-and-copyright
27. https://recime.app/help/en/articles/11626121-can-i-print-or-export-my-recipes
28. https://help.recime.app/en/articles/12381412-how-to-check-the-original-recipe-source
29. https://www.recime.app/dmca-policy
30. https://recime.app/help/en/articles/13571246-what-happens-to-my-saved-recipe-if-the-original-source-is-no-longer-available
31. https://recime.app/help/en/articles/14773584-why-didn-t-my-recipe-import-correctly
32. https://itunes.apple.com/us/rss/customerreviews/page=1/id=1593779280/sortBy=mostRecent/json (and AU/GB feeds)
33. https://www.whistleout.com/CellPhones/Apps/recime-app-review
34. https://www.onclarity.com/leaderboard/insight/recime
35. https://www.netinfluencer.com/jobs/creator-marketing-associate-at-recime/
36. https://www.arbeitnow.com/jobs/companies/recime/ugc-creator-food-content-hamburg-71984
37. https://www.smartcompany.com.au/startupsmart/recime-nyc-new-york-move-recipe-app-startup/
38. https://www.ozbargain.com.au/node/798026 and https://www.ozbargain.com.au/node/827499
39. https://www.recime.app/creators
40. https://fbe.unimelb.edu.au/newsroom/recime-from-the-bcom-to-the-kitchen
41. https://www.similarweb.com/website/recime.app/ and SEMrush US organic (API, 2026-09)
42. https://motionapp.com/library/recime
43. https://sponsorradar.com/brands/recime
44. https://www.capitalbrief.com/briefing/cooking-app-recime-raises-15m-in-seed-round-led-by-even-capital-c46f07d0-5d14-4d13-b10c-817eae74cad6/
45. https://apppricinglab.com/app/apple/1593779280
46. https://www.tiktok.com/legal/page/us/terms-of-service/en
47. https://www.youtube.com/t/terms
48. https://developers.facebook.com/docs/instagram-platform/oembed
49. https://www.recime.app (page source: Next.js, Firebase, GTM, Intercom, Singular, Meta pixel)
50. https://www.recime.app/privacy-policy
51. https://recime.app/help/en/articles/11625001-import-from-youtube
52. https://recime.app/help/en/articles/11625032-import-from-pinterest
53. https://www.recipeone.app/blog/recime-app-review and https://www.plantoeat.com/blog/2025/01/recime-app-review-pros-and-cons/
54. https://www.aol.com/news/backlash-food-bloggers-website-aimed-173500434.html
55. https://archive.factordaily.com/?p=2376
56. https://suequinn.substack.com/p/who-really-owns-recipes
57. https://twosleevers.com/stop-complaining-about-free-recipes/
58. https://help.stashcook.com/en/articles/12204024-how-stashcook-handles-recipe-copyright
59. https://www.deglaze.app/blog/save-recipes-from-video
60. https://resurfy.com/guides/best-apps-to-save-recipes-from-instagram/
61. https://www.anyrecipe.app/blog/best-recipe-apps-iphone
62. https://learn.plantoeat.com/doc/import-recipes-from-instagram
63. https://mealie.io/documentation/getting-started/features/
64. https://fortune.com/2025/11/26/ai-slop-recipes-thanksgiving-food-blog-collapse-traffic
