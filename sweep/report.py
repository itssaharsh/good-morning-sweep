"""Numbers for the person and for the post: what was found, what was freed, and the model alone against the guard."""
from collections import Counter

from . import guard
from .db import get_meta


def gb(b):
    return f"{(b or 0) / 1e9:.2f} GB"


def summary(con):
    lines = []
    f = con.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM files").fetchone()
    lines.append(f"Files copied from {get_meta(con, 'device', 'the phone')}: {f['n']} ({gb(f['b'])})")
    for r in con.execute("SELECT folder, COUNT(*) n, SUM(bytes) b FROM files GROUP BY folder ORDER BY b DESC"):
        lines.append(f"  {r['folder']:14s} {r['n']:6d}  {gb(r['b'])}")
    v = con.execute("SELECT COUNT(*) n, AVG(ms) ms, SUM(ms) total FROM verdicts WHERE kind IS NOT NULL").fetchone()
    if v["n"]:
        lines.append(f"Gemma answered for {v['n']} files: {v['ms'] / 1000:.1f} s per picture on average, "
                     f"{(v['total'] or 0) / 3.6e6:.1f} h of model time")
    kinds = Counter(r["kind"] for r in con.execute("SELECT kind FROM verdicts"))
    lines.append("  kinds: " + ", ".join(f"{k or 'unreadable'} {n}" for k, n in kinds.most_common()))
    lines.append("Suggestions:")
    for b, title, pre in guard.BUCKETS:
        r = con.execute("SELECT COUNT(*) n, COALESCE(SUM(f.bytes),0) b FROM suggestions s JOIN files f ON f.id=s.file_id "
                        "WHERE s.bucket=?", (b,)).fetchone()
        if r["n"]:
            lines.append(f"  {title:24s} {r['n']:6d}  {gb(r['b'])}  {'(ticked)' if pre else '(not ticked)'}")
    d = con.execute("SELECT SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) del, "
                    "SUM(CASE WHEN action='keep' THEN 1 ELSE 0 END) keep FROM decisions").fetchone()
    if d["del"] is not None:
        unticked = con.execute("SELECT COUNT(*) n FROM decisions d JOIN suggestions s ON s.file_id=d.file_id "
                               "WHERE s.preselected=1 AND d.action='keep'").fetchone()["n"]
        added = con.execute("SELECT COUNT(*) n FROM decisions d JOIN suggestions s ON s.file_id=d.file_id "
                            "WHERE s.preselected=0 AND d.action='delete'").fetchone()["n"]
        lines.append(f"Review: {d['del']} to delete, {d['keep']} kept ({unticked} ticked ones kept by the person, "
                     f"{added} unticked ones added)")
    x = con.execute("SELECT SUM(ok) n, SUM(CASE WHEN ok=1 THEN f.bytes ELSE 0 END) b FROM deletions d "
                    "JOIN files f ON f.id=d.file_id").fetchone()
    if x["n"]:
        lines.append(f"Deleted from the phone: {x['n']} files, {gb(x['b'])}")
    fb, fa = get_meta(con, "free_before"), get_meta(con, "free_after")
    if fb and fa:
        lines.append(f"Phone free space: {gb(float(fb))} before, {gb(float(fa))} after")
    return "\n".join(lines)


def versus(con):
    """Model alone vs the guard, on the files a person labelled (junk or keep). The error that hurts first:
    pictures the person wants kept that would have been deleted."""
    rows = con.execute("""
        SELECT l.file_id, l.label, v.kind, v.words, v.people, v.screen, s.bucket, s.preselected, f.folder
        FROM labels l JOIN files f ON f.id=l.file_id LEFT JOIN verdicts v ON v.file_id=l.file_id
        LEFT JOIN suggestions s ON s.file_id=l.file_id""").fetchall()
    if not rows:
        return None
    res = {"n": len(rows), "junk": sum(r["label"] == "junk" for r in rows)}
    for name, says in (("model alone", lambda r: guard.model_alone(r)),
                       ("with the guard (ticked)", lambda r: bool(r["bucket"]) and bool(r["preselected"])),
                       ("with the guard (any suggestion)", lambda r: bool(r["bucket"]))):
        hurt = sum(1 for r in rows if r["label"] == "keep" and says(r))
        caught = sum(1 for r in rows if r["label"] == "junk" and says(r))
        res[name] = {"keepers_deleted": hurt, "junk_found": caught}
    return res


def versus_text(res):
    if not res:
        return "No labels yet."
    out = [f"Labelled sample: {res['n']} pictures ({res['junk']} junk, {res['n'] - res['junk']} keep)",
           f"{'':34s}{'pictures to keep, deleted':>28s}{'junk found':>14s}"]
    for k in ("model alone", "with the guard (ticked)", "with the guard (any suggestion)"):
        r = res[k]
        out.append(f"{k:34s}{r['keepers_deleted']:>28d}{r['junk_found']:>9d} of {res['junk']}")
    return "\n".join(out)
