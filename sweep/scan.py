"""The slow part: Gemma looks at every WhatsApp picture, video frame and screenshot, one at a time.

Each answer is written to the database the moment it arrives, so the laptop can be closed or the run stopped at any
point; the next run starts where this one stopped. Identical files are asked about once. Biggest files go first, so
the first hour already finds most of the space."""
import time

from . import checks, media, model
from .db import now

MODEL_FOLDERS = ("wa_images", "wa_sent", "wa_video", "wa_video_sent", "screenshots")


def pending(con):
    """Files that still need the model: one per sha1, WhatsApp and screenshot folders always, camera photos only
    when the code already finds them blurry or black. Biggest first."""
    rows = con.execute(f"""
        SELECT f.*, c.blur, c.dark FROM files f LEFT JOIN checks c ON c.file_id = f.id
        WHERE f.sha1 IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM verdicts v JOIN files g ON g.id = v.file_id WHERE g.sha1 = f.sha1 AND v.kind IS NOT NULL)
          AND (f.folder IN ({",".join("?" * len(MODEL_FOLDERS))})
               OR (f.folder = 'camera' AND (c.blur < ? OR c.dark < ?)))
        ORDER BY f.bytes DESC""", (*MODEL_FOLDERS, checks.BLUR_FLAG, checks.DARK_FLAG)).fetchall()
    one, seen = [], set()
    for r in rows:
        if r["sha1"] not in seen:
            seen.add(r["sha1"])
            one.append(r)
    return one


def store(con, sha1, v):
    """Write one answer for every file with these bytes."""
    ids = [r["id"] for r in con.execute("SELECT id FROM files WHERE sha1=?", (sha1,))]
    with con:
        for fid in ids:
            con.execute("""INSERT INTO verdicts(file_id, kind, words, people, screen, raw, ms, error, model, at)
                           VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET kind=excluded.kind,
                           words=excluded.words, people=excluded.people, screen=excluded.screen, raw=excluded.raw,
                           ms=excluded.ms, error=excluded.error, model=excluded.model, at=excluded.at""",
                        (fid, v["kind"], v["words"], None if v["people"] is None else int(v["people"]), v["screen"],
                         v["raw"], v["ms"], v["error"], model.MODEL_NAME, now()))
    return len(ids)


def run(con, limit=None, url=model.MODEL_URL, classify=model.classify, log=print, every=10):
    todo = pending(con)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    if not total:
        log("Nothing left to look at.")
        return 0
    log(f"{total} pictures for Gemma to look at (biggest first). Stop any time; the next run continues.")
    t0, done, ms_sum = time.time(), 0, 0
    for r in todo:
        im = media.open_picture(r["local_path"], max_side=768)
        if im is None:
            v = {"kind": None, "words": "", "people": None, "screen": "", "raw": "", "ms": 0,
                 "error": "could not open the picture"}
        else:
            v = classify(media.jpeg_bytes(im), url=url, log=log)
        store(con, r["sha1"], v)
        done += 1
        ms_sum += v["ms"] or 0
        if done % every == 0 or done == total:
            rate = (time.time() - t0) / done
            left = (total - done) * rate
            log(f"{done}/{total}  {rate:4.1f} s each  about {left / 3600:4.1f} h left  last: {v['kind']} "
                f"“{(v['words'] or '')[:40]}”")
    return done
