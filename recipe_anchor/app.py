"""
app.py — FastAPI surface.

  POST /recipe/from-url    {"url": "..."}        -> RecipeDoc JSON
  POST /recipe/from-text   {"text": "...", ...}  -> RecipeDoc JSON
  POST /recipe/render-url  {"url": "..."}        -> rendered HTML page
  POST /recipe/render-doc  <RecipeDoc JSON>      -> rendered HTML page (no LLM)

Run:  uvicorn recipe_anchor.app:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .models import RecipeDoc
from .pipeline import build_from_url, build_from_text
from .render import render_html

app = FastAPI(title="Recipe Anchor")


class UrlIn(BaseModel):
    url: str


class TextIn(BaseModel):
    text: str
    source_url: str | None = None


@app.post("/recipe/from-url", response_model=RecipeDoc)
def from_url(body: UrlIn) -> RecipeDoc:
    try:
        return build_from_url(body.url)
    except Exception as e:  # surface pipeline errors cleanly
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/recipe/from-text", response_model=RecipeDoc)
def from_text(body: TextIn) -> RecipeDoc:
    return build_from_text(body.text, source_url=body.source_url)


@app.post("/recipe/render-url", response_class=HTMLResponse)
def render_url(body: UrlIn) -> str:
    return render_html(build_from_url(body.url))


@app.post("/recipe/render-doc", response_class=HTMLResponse)
def render_doc(doc: RecipeDoc) -> str:
    """Render an already-built doc — useful for iterating on the template without LLM calls."""
    return render_html(doc)
