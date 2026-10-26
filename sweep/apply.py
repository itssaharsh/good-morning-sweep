"""Delete what the review approved, from the phone only. The laptop copy of every file stays.

Each file is deleted only when all of these hold:
- a review was saved after the suggestions were last computed;
- the connected phone is the phone this workspace was copied from;
- the laptop copy exists and has the same SHA-1 as when it was indexed;
- for an exact copy or a look-alike, the copy that is kept is on the phone and is not being deleted;
- on the phone, the file still has that same SHA-1 (checked by the phone right before it removes it)."""
from pathlib import Path

from . import media
from .db import get_meta, now, set_meta


class NotReviewed(RuntimeError):
    pass


class WrongPhone(RuntimeError):
    pass


def approved(con, labels=False):
    """The approved rows, with the reasons any of them must stay. labels=True adds every picture the person labelled
    junk on the label page (an explicit decision of theirs, made one picture at a time)."""
    rows = con.execute("""
        SELECT f.id, f.phone_path, f.local_path, f.bytes, f.sha1, s.keep_id, s.rule
        FROM decisions d JOIN files f ON f.id = d.file_id
        JOIN suggestions s ON s.file_id = d.file_id
        LEFT JOIN deletions x ON x.file_id = d.file_id
        WHERE d.action = 'delete' AND COALESCE(f.gone, 0) = 0 AND (x.ok IS NULL OR x.ok = 0)""").fetchall()
    if labels:
        seen = {r["id"] for r in rows}
        rows += [r for r in con.execute("""
            SELECT f.id, f.phone_path, f.local_path, f.bytes, f.sha1, NULL AS keep_id, 'L1-labelled-junk' AS rule
            FROM labels l JOIN files f ON f.id = l.file_id
            LEFT JOIN deletions x ON x.file_id = l.file_id
            WHERE l.label = 'junk' AND COALESCE(f.gone, 0) = 0 AND (x.ok IS NULL OR x.ok = 0)""") if r["id"] not in seen]
    return rows


def approve_ticked(con):
    """Record 'delete' for every ticked suggestion with no decision yet (the person said so outside the page)."""
    with con:
        n = con.execute("""INSERT INTO decisions(file_id, action, at) SELECT file_id, 'delete', ? FROM suggestions
                           WHERE preselected = 1 AND file_id NOT IN (SELECT file_id FROM decisions)""", (now(),)).rowcount
    set_meta(con, "review_saved_at", now())
    return n


def check_review(con):
    saved = get_meta(con, "review_saved_at")
    if not saved:
        raise NotReviewed("No review saved yet. Open the review page (sweep review) and choose what to delete first.")
    sug = get_meta(con, "suggested_at")
    if sug and float(sug) > float(saved):
        raise NotReviewed("The suggestions changed after the review was saved. Open the review page again.")


def apply(con, phone, scratch_dir=None, log=print, dry_run=False, labels=False):
    check_review(con)
    if not dry_run:
        want = get_meta(con, "serial")
        if not want:
            raise WrongPhone("This workspace was not copied from a phone (sweep import), so nothing can be deleted.")
        if getattr(phone, "serial", None) != want:
            raise WrongPhone(f"The connected phone ({getattr(phone, 'serial', None)}) is not the phone this review is "
                             f"for ({want}). Plug in the right phone.")
    rows = approved(con, labels=labels)
    ids = {r["id"] for r in rows}
    keep_paths = {}
    for k in {r["keep_id"] for r in rows if r["keep_id"]}:
        kr = con.execute("SELECT f.phone_path, COALESCE(f.gone,0) gone, x.ok FROM files f LEFT JOIN deletions x "
                         "ON x.file_id=f.id WHERE f.id=?", (k,)).fetchone()
        if kr and not kr["gone"] and not kr["ok"]:
            keep_paths[k] = kr["phone_path"]
    held = []
    ready = []
    for r in rows:
        if r["keep_id"] and (r["keep_id"] in ids or r["keep_id"] not in keep_paths):
            held.append((r, "the copy it was kept for is being deleted or is gone"))
            continue
        lp = Path(r["local_path"])
        if not (lp.exists() and lp.stat().st_size == r["bytes"] and media.sha1(lp) == r["sha1"]):
            held.append((r, "its laptop copy is missing or different"))
            continue
        ready.append(r)
    if ready and not dry_run:
        need = sorted({keep_paths[r["keep_id"]] for r in ready if r["keep_id"]})
        there = phone.present(need, scratch_dir) if need else set()
        still = []
        for r in ready:
            if r["keep_id"] and keep_paths[r["keep_id"]] not in there:
                held.append((r, "the copy it was kept for is not on the phone"))
            else:
                still.append(r)
        ready = still
    for r, why in held[:5]:
        log(f"kept on the phone: {r['phone_path'].rsplit('/', 1)[-1]} ({why})")
    if len(held) > 5:
        log(f"... and {len(held) - 5} more kept on the phone for the same reasons")
    if dry_run or not ready:
        return {"deleted": 0, "freed": 0, "held": len(held), "failed": 0,
                "would_delete": len(ready), "would_free": sum(r["bytes"] for r in ready)}

    if hasattr(phone, "can_hash") and not phone.can_hash():
        raise WrongPhone("This phone has no sha1sum, so the tool cannot confirm each file before deleting it. "
                         "Nothing was deleted.")
    before = phone.free_bytes()
    if not get_meta(con, "free_before"):
        set_meta(con, "free_before", before)
    by_path = {r["phone_path"]: r for r in ready}
    tally = {"deleted": 0, "freed": 0, "failed": 0}

    def record(batch):   # written after every batch, so a failure later loses nothing
        with con:
            for p, (ok, msg) in batch.items():
                r = by_path[p]
                con.execute("INSERT INTO deletions(file_id, ok, message, at) VALUES(?,?,?,?) ON CONFLICT(file_id) DO "
                            "UPDATE SET ok=excluded.ok, message=excluded.message, at=excluded.at",
                            (r["id"], 1 if ok else 0, msg, now()))
                if ok:
                    tally["deleted"] += 1
                    tally["freed"] += r["bytes"]
                else:
                    tally["failed"] += 1
    phone.delete([(r["phone_path"], r["sha1"]) for r in ready], scratch_dir, on_batch=record)
    after = phone.free_bytes()
    set_meta(con, "free_after", after)
    log(f"Deleted {tally['deleted']} files ({tally['freed'] / 1e9:.2f} GB). Phone free space: {before / 1e9:.2f} GB "
        f"-> {after / 1e9:.2f} GB. {tally['failed']} not deleted, {len(held)} kept for safety.")
    return dict(tally, held=len(held), free_before=before, free_after=after)
