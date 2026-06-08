"""
products.py — deterministic product lookup by equipment category.

This is the runtime half of the buying-intelligence split: the LLM only ever emits an
equipment `category` (a context gate). Code resolves that category to concrete products
and affiliate links. No e-commerce data is ever placed in an LLM prompt.

Replace CATALOG with your real product source / affiliate feed. The shape is what the
template consumes: a list of up to three picks per category.
"""
from __future__ import annotations
from typing import TypedDict


class Product(TypedDict):
    name: str
    desc: str
    price: str
    stars: int      # 0-5
    emoji: str      # placeholder thumbnail; swap for `image_url` in production
    url: str        # affiliate/product url (resolved here, never via the LLM)


# House-brand placeholder catalog. Three picks per category.
CATALOG: dict[str, list[Product]] = {
    "bowl": [
        {"name": "Bestcooks Club 6-qt Steel Bowl", "desc": "Wide mouth, flat base that won’t tip.", "price": "$24", "stars": 5, "emoji": "🥣", "url": "#"},
        {"name": "Bestcooks Club Glass Bowl, 5 qt", "desc": "Tempered glass; watch the soak.", "price": "$19", "stars": 4, "emoji": "🥣", "url": "#"},
        {"name": "Bestcooks Club Nested Prep Set", "desc": "5 / 3 / 1.5 qt, store as one.", "price": "$32", "stars": 5, "emoji": "🥣", "url": "#"},
    ],
    "colander": [
        {"name": "Bestcooks Club 5-qt Footed Colander", "desc": "Stands above the sink water.", "price": "$22", "stars": 5, "emoji": "🧺", "url": "#"},
        {"name": "Bestcooks Club Mesh Strainer, 8 in", "desc": "Catches grit and tiny pasta.", "price": "$16", "stars": 4, "emoji": "🧺", "url": "#"},
        {"name": "Bestcooks Club Collapsible Colander", "desc": "Folds flat for storage.", "price": "$18", "stars": 4, "emoji": "🧺", "url": "#"},
    ],
    "pot": [
        {"name": "Bestcooks Club 8-qt Stockpot", "desc": "Tall sides, fast rolling boil.", "price": "$49", "stars": 5, "emoji": "🍝", "url": "#"},
        {"name": "Bestcooks Club Pasta Pot + Insert", "desc": "Lift the insert to drain.", "price": "$58", "stars": 5, "emoji": "🍝", "url": "#"},
        {"name": "Bestcooks Club 6-qt Everyday Pot", "desc": "Right size for a pound of pasta.", "price": "$39", "stars": 4, "emoji": "🍝", "url": "#"},
    ],
    "pan": [
        {"name": "Bestcooks Club 12-in Sauté Pan + Lid", "desc": "Tri-ply base, steady low heat.", "price": "$89", "stars": 5, "emoji": "🍳", "url": "#"},
        {"name": "Bestcooks Club Ceramic Sauté, 12 in", "desc": "Nonstick — clams release clean.", "price": "$69", "stars": 4, "emoji": "🍳", "url": "#"},
        {"name": "Bestcooks Club Everyday Skillet, 12 in", "desc": "Glass lid, oven-safe.", "price": "$59", "stars": 4, "emoji": "🍳", "url": "#"},
    ],
    "spoon": [
        {"name": "Bestcooks Club Olivewood Spoon", "desc": "Won’t scratch or react.", "price": "$12", "stars": 5, "emoji": "🥄", "url": "#"},
        {"name": "Bestcooks Club Beechwood Set of 3", "desc": "Flat edge reaches the pan bottom.", "price": "$18", "stars": 4, "emoji": "🥄", "url": "#"},
        {"name": "Bestcooks Club Silicone Spoonula", "desc": "Spoon and scraper in one.", "price": "$11", "stars": 4, "emoji": "🥄", "url": "#"},
    ],
    "knife": [
        {"name": "Bestcooks Club 8-in Chef’s Knife", "desc": "Half-bolster, full tang.", "price": "$79", "stars": 5, "emoji": "🔪", "url": "#"},
        {"name": "Bestcooks Club Santoku, 7 in", "desc": "Granton edge for clean slivers.", "price": "$69", "stars": 4, "emoji": "🔪", "url": "#"},
        {"name": "Bestcooks Club Herb Shears", "desc": "Snip parsley over the pan.", "price": "$16", "stars": 4, "emoji": "🔪", "url": "#"},
    ],
    "board": [
        {"name": "Bestcooks Club Edge-Grain Board", "desc": "Roomy, with a juice groove.", "price": "$44", "stars": 5, "emoji": "🪵", "url": "#"},
        {"name": "Bestcooks Club Poly Board Set", "desc": "Dishwasher-safe, color-coded.", "price": "$26", "stars": 4, "emoji": "🪵", "url": "#"},
        {"name": "Bestcooks Club Bamboo Board", "desc": "Light and knife-friendly.", "price": "$22", "stars": 4, "emoji": "🪵", "url": "#"},
    ],
    "tongs": [
        {"name": "Bestcooks Club 12-in Locking Tongs", "desc": "Silicone tips, scalloped grip.", "price": "$14", "stars": 5, "emoji": "🍴", "url": "#"},
        {"name": "Bestcooks Club Pasta Fork", "desc": "Teeth grab strands; port measures a portion.", "price": "$9", "stars": 4, "emoji": "🍴", "url": "#"},
        {"name": "Bestcooks Club Spider Skimmer", "desc": "Scoop pasta into the pan.", "price": "$12", "stars": 5, "emoji": "🍴", "url": "#"},
    ],
    "timer": [
        {"name": "Bestcooks Club Magnetic Digital Timer", "desc": "Loud; sticks to the hood.", "price": "$13", "stars": 5, "emoji": "⏲️", "url": "#"},
        {"name": "Bestcooks Club Triple Timer", "desc": "Track pasta and clams at once.", "price": "$19", "stars": 4, "emoji": "⏲️", "url": "#"},
        {"name": "Bestcooks Club Analog Ring Timer", "desc": "No batteries — just twist.", "price": "$11", "stars": 4, "emoji": "⏲️", "url": "#"},
    ],
    "serving": [
        {"name": "Bestcooks Club Wide Pasta Bowls (4)", "desc": "Warm-safe stoneware.", "price": "$48", "stars": 5, "emoji": "🍽️", "url": "#"},
        {"name": "Bestcooks Club Oval Serving Platter", "desc": "Holds the whole pan.", "price": "$34", "stars": 4, "emoji": "🍽️", "url": "#"},
        {"name": "Bestcooks Club Footed Pasta Coupe", "desc": "Restaurant-style rim.", "price": "$14", "stars": 4, "emoji": "🍽️", "url": "#"},
    ],
    "other": [],
}


def picks_for(category: str) -> list[Product]:
    return CATALOG.get(category, [])[:3]
