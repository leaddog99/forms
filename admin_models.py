"""Tiny declarative admin scaffold — define a model once, get list +
Add/Change/Delete for free.

The goal (owner's words): "a model, a/c/d, and a view template you can plug
in the db." A model describes a DB table — its columns, which are editable,
how each renders — and the generic admin endpoints in save_recipe_api.py
(/admin/{model}...) plus the generic view (forms/admin.html) do the rest.
Adding a new admin-managed table is: append an AdminModel to ADMIN_MODELS.
No new endpoint, no new page.

Safety: the generic endpoints only ever touch tables/columns declared here.
A request naming an unregistered model 404s, and writes are restricted to a
model's whitelisted editable fields — so the generic SQL can never reach an
arbitrary column or table.

NOTE: these endpoints are unauthenticated like the rest of the app today.
That's fine for owner-only/local + tunnel use; add auth before exposing the
admin surface publicly.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AdminField:
    """One column, as the form + list should treat it."""
    name: str
    label: str
    type: str = "text"          # text | textarea | number | bool | select
    required: bool = False
    editable: bool = True        # False => shown in list, never in add/edit form
    default: Any = None
    options: Optional[list] = None   # for type == "select"
    help: str = ""
    in_list: bool = True         # show this column in the list view


@dataclass
class AdminModel:
    """A DB table exposed through the generic admin."""
    name: str                    # url slug (also the table name unless `table` set)
    label: str                   # human title
    fields: list[AdminField]
    table: Optional[str] = None
    pk: str = "id"
    order_by: str = "id"
    create_sql: str = ""          # CREATE TABLE IF NOT EXISTS ...
    seed: list[dict] = field(default_factory=list)  # inserted only if table empty

    def __post_init__(self):
        if self.table is None:
            self.table = self.name

    # --- helpers used by the generic endpoints ---
    def field_map(self) -> dict[str, AdminField]:
        return {f.name: f for f in self.fields}

    def editable_names(self) -> list[str]:
        return [f.name for f in self.fields if f.editable]

    def has_col(self, col: str) -> bool:
        return any(f.name == col for f in self.fields)

    def coerce(self, name: str, value: Any) -> Any:
        """Coerce an incoming form value to the field's storage type, and
        validate select membership. Raises ValueError on bad input."""
        f = self.field_map().get(name)
        if f is None:
            raise ValueError(f"unknown field {name!r}")
        if f.type == "bool":
            if isinstance(value, bool):
                return 1 if value else 0
            return 1 if str(value).strip().lower() in ("1", "true", "yes", "on") else 0
        if f.type == "number":
            if value is None or value == "":
                return 0
            return int(value)
        if f.type == "select":
            sval = "" if value is None else str(value)
            if f.options and sval not in f.options:
                raise ValueError(f"{name}={sval!r} not in {f.options}")
            return sval
        # text / textarea
        return "" if value is None else str(value)

    def schema_json(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "pk": self.pk,
            "fields": [
                {
                    "name": f.name, "label": f.label, "type": f.type,
                    "required": f.required, "editable": f.editable,
                    "default": f.default, "options": f.options,
                    "help": f.help, "in_list": f.in_list,
                }
                for f in self.fields
            ],
        }


# === Registered models ======================================================

# Operations the recipe form runs that have a visible wait; each is a bucket
# of rotating status messages. "general" is the fallback when a specific
# bucket has no enabled messages.
STATUS_MESSAGE_CATEGORIES = [
    "screenshot", "url", "extract", "image", "pdf", "save", "general",
]

_STATUS_SEED = [
    # screenshot — the up-to-45s bookmarklet poll, the big one
    ("screenshot", "Reading the recipe over the chef's shoulder…"),
    ("screenshot", "Bribing the paywall…"),
    ("screenshot", "Arguing with the page's JavaScript…"),
    ("screenshot", "Negotiating with html2canvas…"),
    ("screenshot", "Convincing the browser to hold still for a photo…"),
    ("screenshot", "Wrangling pixels into prose…"),
    ("screenshot", "Still going — good recipes are worth the wait…"),
    # url
    ("url", "Knocking on the website's door…"),
    ("url", "Dodging the cookie banner…"),
    ("url", "Scrolling past the life story…"),
    ("url", "Hunting for the recipe in the ad jungle…"),
    # extract (markdown -> recipe)
    ("extract", "Sniffing out the ingredients…"),
    ("extract", "Translating chef-speak into data…"),
    ("extract", "Counting the teaspoons…"),
    ("extract", "Untangling the instructions…"),
    ("extract", "Asking the AI to taste-test…"),
    # image (vision OCR)
    ("image", "Squinting at the photo…"),
    ("image", "Reading between the food stains…"),
    ("image", "Turning pixels into pantry items…"),
    # pdf
    ("pdf", "Flipping through the pages…"),
    ("pdf", "Decoding the scan…"),
    ("pdf", "Squinting at the fine print…"),
    # save
    ("save", "Filing it in the cookbook…"),
    ("save", "Making it official…"),
    ("save", "Tucking the recipe in for the night…"),
    # general fallback
    ("general", "Working some kitchen magic…"),
    ("general", "Stirring the pot…"),
    ("general", "Almost plated…"),
]

STATUS_MESSAGES_MODEL = AdminModel(
    name="status_messages",
    label="Status Messages",
    order_by="category, sort_order, id",
    create_sql="""
        CREATE TABLE IF NOT EXISTS status_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL DEFAULT 'general',
            message     TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """,
    fields=[
        AdminField("id", "ID", type="number", editable=False),
        AdminField("category", "Category", type="select",
                   options=STATUS_MESSAGE_CATEGORIES, required=True,
                   default="general",
                   help="Which operation's wait this message rotates through."),
        AdminField("message", "Message", type="textarea", required=True,
                   help="Shown while the user waits. Keep it short and fun."),
        AdminField("enabled", "Enabled", type="bool", default=1),
        AdminField("sort_order", "Order", type="number", default=0,
                   help="Lower shows first within a category."),
        AdminField("created_at", "Created", type="text", editable=False, in_list=False),
        AdminField("updated_at", "Updated", type="text", editable=False, in_list=False),
    ],
    seed=[
        {"category": c, "message": m, "enabled": 1, "sort_order": i}
        for i, (c, m) in enumerate(_STATUS_SEED)
    ],
)

ADMIN_MODELS: dict[str, AdminModel] = {
    STATUS_MESSAGES_MODEL.name: STATUS_MESSAGES_MODEL,
}


def get_model(name: str) -> Optional[AdminModel]:
    return ADMIN_MODELS.get(name)


def ensure_admin_tables(conn: sqlite3.Connection) -> None:
    """Create every registered model's table (idempotent) and seed it with
    the model's defaults the first time (only when the table is empty)."""
    for m in ADMIN_MODELS.values():
        if m.create_sql:
            conn.execute(m.create_sql)
        if m.seed:
            count = conn.execute(f"SELECT COUNT(*) FROM {m.table}").fetchone()[0]
            if count == 0:
                ts = _now()
                editable = m.editable_names()
                stamp_created = m.has_col("created_at")
                stamp_updated = m.has_col("updated_at")
                for row in m.seed:
                    cols = [k for k in editable if k in row]
                    vals = [m.coerce(k, row[k]) for k in cols]
                    if stamp_created:
                        cols.append("created_at"); vals.append(ts)
                    if stamp_updated:
                        cols.append("updated_at"); vals.append(ts)
                    placeholders = ", ".join("?" for _ in cols)
                    conn.execute(
                        f"INSERT INTO {m.table} ({', '.join(cols)}) "
                        f"VALUES ({placeholders})",
                        vals,
                    )
                print(f"[ADMIN] seeded {m.table} with {len(m.seed)} rows")
