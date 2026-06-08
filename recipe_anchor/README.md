# recipe_anchor

Turns a recipe URL into a **step-anchored** cook page: each step carries the equipment
to gather (sized from quantities) and the amounts to use (inline, bold, with a metric/
imperial toggle), so the cook never scrolls back to a master list.

## Pipeline

```
URL ──fetch_clean──► cleaned text
                         │
                  Pass 1 │ EXTRACT  (LLM, forced tool emit_recipe)
                         ▼
                  base recipe  (ingredients + raw steps, quantities verbatim)
                         │
                  Pass 2 │ ANCHOR   (LLM, forced tool emit_step_plan)
                         ▼
                  step plan  (equipment+sizes, inline {ingN} tokens, both unit systems)
                         │
                 assemble │  ──►  RecipeDoc  (single source of truth)
                         ▼
                  render_html  ──►  the page
```

Two LLM passes, each a single forced tool call returning JSON that maps onto the Pydantic
models in `models.py`. The prompts live in `prompts.py` and encode every rule we settled
on:

- **Equipment** gets a **size only where size matters** (bowls, pots, pans, colanders,
  serving vessels), inferred from the recipe's quantities; spoons/tongs/timers stay
  unsized. Reused pieces reference the same id with `reused_from_step` and are not
  re-introduced. `equipment_order` is the deduped list in order of first need.
- **Ingredients are inline.** No per-step list; the model rewrites each instruction with
  `{ingN}` tokens (and `{amt:..}` for non-ingredient measures like times). The renderer
  expands these into `<span class="ing">` markup, amount first in clay/bold, then the
  ingredient. Reused ingredients render as a bold noun with no fresh amount. A measure can
  be a vessel ("a bowl of") not just a number.
- **Both unit systems** on every amount, display-ready with cooking roundings
  (`1 lb`→`450 g`, `¼ cup`→`60 ml`). Non-convertible measures (counts, "to taste",
  vessel measures) set `convertible=false`.

## Products are not in the LLM

Per the buying-intelligence split, the model emits only an equipment `category` (a
context gate). `products.py` resolves categories to concrete picks and affiliate URLs.
**No e-commerce data ever enters a prompt.** Swap `CATALOG` for your real feed.

## Run

```bash
pip install fastapi uvicorn anthropic httpx jinja2 pydantic
export ANTHROPIC_API_KEY=...
# set the exact model ids you're entitled to:
export RA_MODEL_EXTRACT=claude-sonnet-4-5
export RA_MODEL_ANCHOR=claude-sonnet-4-5

uvicorn recipe_anchor.app:app --reload
```

Endpoints: `POST /recipe/from-url`, `/recipe/from-text`, `/recipe/render-url`,
`/recipe/render-doc` (render an existing doc with no LLM call — handy for template work).

Render the bundled fixture with no API key:

```bash
python -m recipe_anchor.example_vongole > out.html   # see sample_output.html
```

## Files

| file | role |
|------|------|
| `models.py` | Schema.org-extended `RecipeDoc` — the single source of truth |
| `prompts.py` | the two system prompts + forced-tool schemas |
| `pipeline.py` | fetch → clean → extract → anchor → assemble |
| `render.py` | token expansion + Jinja render |
| `templates/recipe.html.j2` | the page (CSS embedded, matches the prototype) |
| `products.py` | deterministic category→product catalog (context-gate seam) |
| `app.py` | FastAPI surface |
| `example_vongole.py` / `sample_output.html` | golden fixture + rendered proof |

## Production seams

- **Images.** Product thumbnails are emoji placeholders; replace `emoji` with a re-hosted
  `image_url` (same local re-hosting you do for recipe images at extraction time, to dodge
  hotlink protection).
- **Unit toggle in product copy.** Item *sizes* respect the toggle, but free-text product
  descriptions ("12-in", "500°F") don't yet — make those data-driven if it matters.
- **Persistence.** The toggle and checked-step state are session-only in the page; persist
  per-user server-side rather than in browser storage.
- **Extraction input.** `fetch_clean` is a minimal strip; route your bookmarklet-captured
  cleaned HTML / screenshot through the same `extract_base` tool instead for better yield.
```
