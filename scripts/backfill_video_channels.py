"""One-off / re-runnable: give every video recipe its channel.

Rows saved before 2026-10-17 lost `_source.capture` at the model boundary
(SourceInfo had no `capture` field), so the two YouTube recipes in master
carry no kind, no channel and no channel_id — and the channels table, which
is fed from that stamp at save time, cannot see them. This reads each video
row's watch page (the ladder's own fetch_watch), stamps what the page says
about the CHANNEL — kind, label, structure, channel, channel_id, handle —
and creates/refreshes the channel row. It never invents a rung, a door
(`via`) or transcript counts for a row that was saved without them: those
were facts about the capture, and the capture is gone.

    python scripts/backfill_video_channels.py            # apply
    python scripts/backfill_video_channels.py --check    # report only, no writes

Prints every row before and after (memory/feedback_materialize_stored_not_derived):
a row that already carries a channel_id is left alone.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from input.pipeline import channels_lib, vector_store  # noqa: E402
from input.pipeline.db import connect  # noqa: E402
from intake import youtube_source  # noqa: E402
from video_lookup import youtube_id  # noqa: E402

DB = os.path.join(ROOT, "recipes.db")


def main() -> int:
    check = "--check" in sys.argv
    conn = connect(DB)
    vector_store.enable_vec(conn)   # an UPDATE on these tables fires the vec triggers
    channels_lib.ensure_channels_table(conn)
    seen_pages: dict = {}
    n_stamped = n_skipped = n_failed = 0
    for tbl in ("master_recipes", "recipes"):
        rows = conn.execute(
            f"SELECT id, recipe_id, data FROM {tbl} WHERE source_class = 'video' ORDER BY id").fetchall()
        for rid, recipe_id, dj in rows:
            d = json.loads(dj)
            src = d.get("_source") or {}
            cap = dict(src.get("capture") or {})
            url = src.get("originalUrl") or ""
            vid = youtube_id(url)
            print(f"\n{tbl} #{rid} {d.get('name')!r}\n  url: {url}\n  before: capture={json.dumps(cap) if cap else 'ABSENT'}")
            if cap.get("channel_id"):
                print("  skip: already carries a channel_id")
                n_skipped += 1
                if not check:
                    channels_lib.touch_channel(conn, channel_id=cap["channel_id"], name=cap.get("channel") or "",
                                               handle=cap.get("handle") or "")
                continue
            if not vid:
                print("  FAIL: not a YouTube watch URL")
                n_failed += 1
                continue
            info = seen_pages.get(vid)
            if info is None:
                info = youtube_source.fetch_watch(vid, log=lambda m: print("  " + m))
                seen_pages[vid] = info
            if info is None or not info.channel_id:
                print("  FAIL: watch page unreadable or no channelId")
                n_failed += 1
                continue
            cap.setdefault("kind", "youtube")
            cap.setdefault("label", "YouTube")
            cap.setdefault("structure", "video")
            cap["channel"] = cap.get("channel") or info.channel
            cap["channel_id"] = info.channel_id
            if info.handle:
                cap["handle"] = info.handle
            print(f"  after:  capture={json.dumps(cap)}")
            n_stamped += 1
            if check:
                continue
            src["capture"] = cap
            d["_source"] = src
            conn.execute(f"UPDATE {tbl} SET data = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), rid))
            conn.commit()
            channels_lib.touch_channel(conn, channel_id=info.channel_id, name=info.channel, handle=info.handle)
    if not check:
        n = channels_lib.recount_channels(conn)
        print(f"\nchannels recounted: {n}")
        for r in channels_lib.list_channels(conn):
            print(f"  {r['channel_id']}  @{r['handle'] or '?'}  {r['display_name']!r}  "
                  f"master={r['master_recipe_count']} user={r['user_recipe_count']}")
    print(f"\n{'would stamp' if check else 'stamped'} {n_stamped}, skipped {n_skipped}, failed {n_failed}")
    conn.close()
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
