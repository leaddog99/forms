"""channels — the video-platform counterpart of `domains`.

A recipe fetched from a page has a publisher, and the publisher record is a
`domains` row keyed by host. A recipe read off a YouTube video has a CHANNEL,
and a channel handle is not a host: Fallow's recipes live on youtube.com like
everyone else's, so the domain master cannot tell one creator from another.
This table is where a channel's identity, the curator's judgement of it
("never this channel" / "always seat this one") and, from step 3 of the video
lane on, its measured standing live. docs/video-recipes-lane.md §5.

Keyed by the platform's own stable id (YouTube's `UC…` channelId, which
survives renames); `handle` (@FallowChefs) and `display_name` are what the
watch page said when the channel was last seen. Rows are created the first
time a video recipe from the channel is SAVED (`touch_channel`, called from
the save chokepoint) — never from a staged grab that was abandoned — and may
also be created by hand in the editor ahead of any save.

Counts follow the domains convention: the STORED `master_recipe_count` /
`user_recipe_count` are refresh-on-access (stamped when the channel's recipes
panel is opened, `recipes_for_channel`), the LIST endpoint reports live counts
from the recipe tables, and `dishes_ranked` / `mean_position` / `standing`
stay NULL until the lane's rank step stamps them (absent, not zero —
memory/feedback_absent_not_zero).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from input.pipeline.db import connect as db_connect

_DEFAULT_DB = "recipes.db"

PLATFORMS = ("youtube",)

# What the editor (and PATCH /channels/{id}) may write. Measured columns —
# counts, dishes_ranked, mean_position, standing* — are stamped by code only.
EDITABLE_FIELDS = (
    "handle",
    "display_name",
    "url",
    "country",
    "language",
    "story",
    "known_for",
    "notes",
    "harvestable",   # 0 = never admit this channel's videos to a dish lane
    "pinned",        # 1 = always seat this channel's video when it is a candidate
)

# Where a recipe row says which channel it came from. Stamped on
# `_source.capture` by the YouTube ladder (intake/youtube_source.py) and
# carried through the save (recipe_model.SourceInfo.capture).
_CAPTURE_CHANNEL = "json_extract(data,'$._source.capture.channel_id')"

_YT_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canon_channel_id(channel_id: str) -> str:
    return (channel_id or "").strip()


def is_youtube_channel_id(channel_id: str) -> bool:
    return bool(_YT_ID_RE.match(canon_channel_id(channel_id)))


def channel_url(channel_id: str, handle: str = "", platform: str = "youtube") -> str:
    """The channel's public address. The handle form reads better and is what
    YouTube itself links; the id form always resolves, rename or not."""
    if platform == "youtube":
        h = (handle or "").strip().lstrip("@")
        if h:
            return f"https://www.youtube.com/@{h}"
        cid = canon_channel_id(channel_id)
        return f"https://www.youtube.com/channel/{cid}" if cid else ""
    return ""


def ensure_channels_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id          TEXT PRIMARY KEY,
            platform            TEXT NOT NULL DEFAULT 'youtube',
            handle              TEXT NOT NULL DEFAULT '',
            display_name        TEXT NOT NULL DEFAULT '',
            url                 TEXT NOT NULL DEFAULT '',
            country             TEXT,
            language            TEXT,
            story               TEXT,
            known_for           TEXT,
            notes               TEXT,
            harvestable         INTEGER NOT NULL DEFAULT 1,
            pinned              INTEGER NOT NULL DEFAULT 0,
            dishes_ranked       INTEGER,
            mean_position       REAL,
            standing            REAL,
            standing_method     TEXT,
            standing_at         TEXT,
            master_recipe_count INTEGER NOT NULL DEFAULT 0,
            user_recipe_count   INTEGER NOT NULL DEFAULT 0,
            counts_updated_at   TEXT,
            first_seen_at       TEXT,
            last_seen_at        TEXT,
            created_at          TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_handle ON channels(platform, handle)")


# --- live counts ----------------------------------------------------------

def _live_counts(conn: sqlite3.Connection, table: str) -> dict:
    """{channel_id: n} for one recipe table. The generated `source_class`
    column is indexed, so the JSON extract runs over the video rows only."""
    try:
        return dict(conn.execute(
            f"SELECT {_CAPTURE_CHANNEL}, COUNT(*) FROM {table} "
            f"WHERE source_class = 'video' AND {_CAPTURE_CHANNEL} IS NOT NULL "
            f"GROUP BY 1"))
    except sqlite3.OperationalError:
        # A database without the source_class column yet (pre-step-1 copy).
        return dict(conn.execute(
            f"SELECT {_CAPTURE_CHANNEL}, COUNT(*) FROM {table} "
            f"WHERE {_CAPTURE_CHANNEL} IS NOT NULL GROUP BY 1"))


def _rows(conn: sqlite3.Connection):
    """A cursor that yields dict-able rows WITHOUT changing the caller's
    connection: the save chokepoint hands us its own connection mid-save."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    ensure_channels_table(conn)
    rows = [dict(r) for r in _rows(conn).execute(
        "SELECT * FROM channels ORDER BY display_name COLLATE NOCASE, handle COLLATE NOCASE"
    ).fetchall()]
    live_m = _live_counts(conn, "master_recipes")
    live_u = _live_counts(conn, "recipes")
    for c in rows:
        c["master_recipe_count"] = live_m.get(c["channel_id"], 0)
        c["user_recipe_count"] = live_u.get(c["channel_id"], 0)
    return rows


def get_channel(conn: sqlite3.Connection, channel_id: str) -> Optional[dict]:
    ensure_channels_table(conn)
    row = _rows(conn).execute("SELECT * FROM channels WHERE channel_id = ?",
                              (canon_channel_id(channel_id),)).fetchone()
    return dict(row) if row else None


def channel_exists(conn: sqlite3.Connection, channel_id: str) -> bool:
    ensure_channels_table(conn)
    return bool(conn.execute("SELECT 1 FROM channels WHERE channel_id = ?",
                             (canon_channel_id(channel_id),)).fetchone())


def _clean_fields(fields: dict) -> dict:
    out = {k: fields.get(k) for k in EDITABLE_FIELDS if k in fields}
    if "handle" in out:
        out["handle"] = (out["handle"] or "").strip().lstrip("@")
    for k in ("harvestable", "pinned"):
        if k in out:
            out[k] = 1 if out[k] in (1, True, "1", "true", "on") else 0
    for k in ("display_name", "url"):
        if k in out and out[k] is None:
            out[k] = ""
    return out


def create_channel(conn: sqlite3.Connection, channel_id: str, fields: dict) -> dict:
    """A curator-created row. Raises ValueError on a blank, malformed or
    duplicate id. Unknown keys are ignored."""
    cid = canon_channel_id(channel_id)
    if not cid:
        raise ValueError("channel_id is required")
    platform = (fields.get("platform") or "youtube").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform {platform!r}")
    if platform == "youtube" and not is_youtube_channel_id(cid):
        raise ValueError("A YouTube channel id looks like UC… (24 characters)")
    ensure_channels_table(conn)
    if channel_exists(conn, cid):
        raise ValueError(f"Channel '{cid}' already exists")
    now = _now()
    payload = _clean_fields(fields)
    payload.setdefault("display_name", "")
    payload.setdefault("handle", "")
    if not (payload.get("url") or "").strip():
        payload["url"] = channel_url(cid, payload.get("handle", ""), platform)
    cols = ["channel_id", "platform", "created_at", "updated_at", *payload.keys()]
    vals = [cid, platform, now, now, *payload.values()]
    conn.execute(f"INSERT INTO channels ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    conn.commit()
    return get_channel(conn, cid)


def update_channel(conn: sqlite3.Connection, channel_id: str, fields: dict) -> dict:
    """Patch an existing row. Only EDITABLE_FIELDS are written."""
    cid = canon_channel_id(channel_id)
    if not channel_exists(conn, cid):
        raise ValueError(f"Channel '{cid}' not found")
    sets = _clean_fields(fields)
    if not sets:
        return get_channel(conn, cid)
    sets["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE channels SET {assignments} WHERE channel_id = ?", [*sets.values(), cid])
    conn.commit()
    return get_channel(conn, cid)


def delete_channel(conn: sqlite3.Connection, channel_id: str) -> bool:
    """Removes the channel's master record only. Its recipes keep their
    capture stamp; the row comes back on the next save from the channel."""
    ensure_channels_table(conn)
    cur = conn.execute("DELETE FROM channels WHERE channel_id = ?", (canon_channel_id(channel_id),))
    conn.commit()
    return bool(cur.rowcount)


def touch_channel(conn: sqlite3.Connection, *, channel_id: str, name: str = "",
                  handle: str = "", platform: str = "youtube") -> Optional[dict]:
    """A video recipe from this channel was just saved: make sure the channel
    row exists and refresh what the watch page said about it. Creates the row
    on first sight. Fills a blank display_name / handle / url; never
    overwrites a curator's edit (memory/feedback_materialize_stored_not_derived).
    Returns the row, or None for an unusable id."""
    cid = canon_channel_id(channel_id)
    if not cid:
        return None
    ensure_channels_table(conn)
    now = _now()
    name = (name or "").strip()
    handle = (handle or "").strip().lstrip("@")
    row = get_channel(conn, cid)
    if row is None:
        conn.execute(
            "INSERT INTO channels (channel_id, platform, handle, display_name, url, "
            "first_seen_at, last_seen_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, platform, handle, name, channel_url(cid, handle, platform), now, now, now, now))
        conn.commit()
        return get_channel(conn, cid)
    sets = {"last_seen_at": now}
    if name and not (row.get("display_name") or "").strip():
        sets["display_name"] = name
    if handle and not (row.get("handle") or "").strip():
        sets["handle"] = handle
    if not (row.get("url") or "").strip():
        sets["url"] = channel_url(cid, handle or row.get("handle") or "", platform)
    if not row.get("first_seen_at"):
        sets["first_seen_at"] = now
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE channels SET {assignments} WHERE channel_id = ?", [*sets.values(), cid])
    conn.commit()
    return get_channel(conn, cid)


def recipes_for_channel(conn: sqlite3.Connection, channel_id: str) -> dict:
    """Live lookup of the recipes saved from this channel, master vs user —
    the same shape `domains_lib.recipes_for_domain` returns, so the editor's
    browse pickers are the same component. Stores the two counts back on the
    row (refresh-on-access); the lists themselves are not stored."""
    cid = canon_channel_id(channel_id)
    result = {"channel_id": cid, "master": {"count": 0, "items": []}, "user": {"count": 0, "items": []}}
    for tbl, bucket in (("master_recipes", "master"), ("recipes", "user")):
        try:
            rows = conn.execute(
                f"SELECT recipe_id, json_extract(data,'$.name'), json_extract(data,'$._source.capture.rung') "
                f"FROM {tbl} WHERE {_CAPTURE_CHANNEL} = ?", (cid,)).fetchall()
        except sqlite3.OperationalError:
            continue
        items = [{"recipe_id": r[0], "name": r[1] or "(no title)", "rung": r[2]} for r in rows]
        items.sort(key=lambda x: x["name"].lower())
        result[bucket] = {"count": len(items), "items": items}
    try:
        ensure_channels_table(conn)
        if channel_exists(conn, cid):
            conn.execute(
                "UPDATE channels SET master_recipe_count = ?, user_recipe_count = ?, counts_updated_at = ? "
                "WHERE channel_id = ?",
                (result["master"]["count"], result["user"]["count"], _now(), cid))
            conn.commit()
    except Exception:
        pass
    return result


def recount_channels(conn: sqlite3.Connection) -> int:
    """Stamp the stored counts on every row from the recipe tables (the
    backfill's closing step). Returns the number of rows updated."""
    ensure_channels_table(conn)
    live_m = _live_counts(conn, "master_recipes")
    live_u = _live_counts(conn, "recipes")
    now = _now()
    n = 0
    for (cid,) in conn.execute("SELECT channel_id FROM channels").fetchall():
        conn.execute(
            "UPDATE channels SET master_recipe_count = ?, user_recipe_count = ?, counts_updated_at = ? "
            "WHERE channel_id = ?", (live_m.get(cid, 0), live_u.get(cid, 0), now, cid))
        n += 1
    conn.commit()
    return n


# --- resolving a pasted reference ------------------------------------------
# The editor's "add a channel" takes whatever the curator has: the channel
# URL, the @handle, a watch URL, or the UC… id itself. One read of the public
# page gives the id (no API key, no quota); a watch URL yields its channel.

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UC_IN_PAGE_RE = re.compile(r'"(?:externalId|channelId)":"(UC[0-9A-Za-z_-]{22})"')
# A channel page's canonical link is the /channel/UC… form; the handle is in
# the page data ("canonicalBaseUrl":"/@FallowChefs", 2026-10-17 probe).
_HANDLE_IN_PAGE_RE = re.compile(r'"canonicalBaseUrl":"/@([^"/?#]+)"')
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


def resolve_youtube_channel(ref: str, *, timeout: float = 12.0) -> Optional[dict]:
    """{channel_id, handle, display_name} for a pasted reference, or None
    when the page could not be read or names no channel."""
    import html as _html
    import requests
    ref = (ref or "").strip()
    if not ref:
        return None
    # A watch URL names its channel in the player data; the ladder's own
    # reader returns the channel's name, id and handle in one fetch.
    from video_lookup import youtube_id
    vid = youtube_id(ref) if ("youtube.com/" in ref or "youtu.be/" in ref) else ""
    if vid:
        from intake.youtube_source import fetch_watch
        info = fetch_watch(vid, log=lambda _m: None)
        if info is None or not info.channel_id:
            return None
        return {"channel_id": info.channel_id, "handle": info.handle, "display_name": info.channel}
    if is_youtube_channel_id(ref):
        url = f"https://www.youtube.com/channel/{ref}"
    elif ref.startswith("@"):
        url = f"https://www.youtube.com/{ref}"
    elif "youtube.com/" in ref or "youtu.be/" in ref:
        url = ref if ref.lower().startswith("http") else "https://" + ref
    else:
        url = f"https://www.youtube.com/@{ref}"
    try:
        r = requests.get(url, params={"hl": "en"},
                         headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                         cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    m = _UC_IN_PAGE_RE.search(r.text)
    if not m:
        return None
    h = _HANDLE_IN_PAGE_RE.search(r.text)
    t = _OG_TITLE_RE.search(r.text)
    return {"channel_id": m.group(1), "handle": h.group(1) if h else "",
            "display_name": _html.unescape(t.group(1)).strip() if t else ""}


def get_unharvestable_channel_ids(db_path: str = _DEFAULT_DB) -> set:
    """Channels the curator has said never to admit to a dish lane. Read by
    the harvest admission step (video lane step 3); cheap enough to run per
    refresh, no cache."""
    conn = db_connect(db_path)
    try:
        ensure_channels_table(conn)
        return {r[0] for r in conn.execute("SELECT channel_id FROM channels WHERE harvestable = 0")}
    finally:
        conn.close()
