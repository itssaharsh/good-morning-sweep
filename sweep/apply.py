"""Delete what the review approved, from the phone only. The laptop copy of every file stays."""
from pathlib import Path

from .db import get_meta, now, set_meta


class NotReviewed(RuntimeError):
    pass


def apply(con, phone, log=print, dry_run=False):
    if not get_meta(con, "review_saved_at"):
        raise NotReviewed("No review saved yet. Open the review page (sweep review) and choose what to delete first.")
    rows = con.execute("""
        SELECT f.id, f.phone_path, f.local_path, f.bytes FROM decisions d JOIN files f ON f.id = d.file_id
        JOIN suggestions s ON s.file_id = d.file_id
        LEFT JOIN deletions x ON x.file_id = d.file_id
        WHERE d.action = 'delete' AND (x.ok IS NULL OR x.ok = 0)""").fetchall()
    # The backup must exist before the phone's copy goes.
    ready = [r for r in rows if Path(r["local_path"]).exists() and Path(r["local_path"]).stat().st_size == r["bytes"]]
    skipped = len(rows) - len(ready)
    if skipped:
        log(f"{skipped} files skipped: their laptop copy is missing or incomplete, so the phone's copy stays.")
    if not ready:
        return {"deleted": 0, "freed": 0, "skipped": skipped, "failed": 0}
    if dry_run:
        return {"deleted": 0, "freed": sum(r["bytes"] for r in ready), "skipped": skipped, "failed": 0,
                "would_delete": len(ready)}
    before = phone.free_bytes()
    if not get_meta(con, "free_before"):
        set_meta(con, "free_before", before)
    result = phone.delete([r["phone_path"] for r in ready])
    deleted = freed = failed = 0
    with con:
        for r in ready:
            ok, msg = result.get(r["phone_path"], (False, "no answer"))
            con.execute("INSERT INTO deletions(file_id, ok, message, at) VALUES(?,?,?,?) ON CONFLICT(file_id) DO "
                        "UPDATE SET ok=excluded.ok, message=excluded.message, at=excluded.at",
                        (r["id"], 1 if ok else 0, msg, now()))
            if ok:
                deleted += 1
                freed += r["bytes"]
            else:
                failed += 1
    after = phone.free_bytes()
    set_meta(con, "free_after", after)
    log(f"Deleted {deleted} files ({freed / 1e9:.2f} GB). Phone free space: {before / 1e9:.2f} GB -> "
        f"{after / 1e9:.2f} GB. {failed} failed, {skipped} skipped.")
    return {"deleted": deleted, "freed": freed, "skipped": skipped, "failed": failed,
            "free_before": before, "free_after": after}
