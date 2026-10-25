"""Score the sweep against a person's own clean-up of the same phone.

Two workspaces from one phone:
- the bin: what the person deleted by hand (the gallery's Recently deleted bin, copied with `sweep pull --bin`);
- the phone: what they kept (copied with `sweep pull`).
Each is scanned once with the same frozen model and rules. For every picture this prints what the person did, what
Gemma alone would do (delete anything it calls a forward or a mistake) and what Gemma with the rules would do (a ticked
suggestion), and writes eval/results.md with counts only (no pictures, no names).

    python eval/compare.py ~/GoodMorningSweep/962c0a0b-bin/sweep.db ~/GoodMorningSweep/962c0a0b/sweep.db
"""
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sweep import guard  # noqa: E402

MODEL_FOLDERS = ("wa_images", "wa_sent", "wa_video", "wa_video_sent", "screenshots")


def rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = con.execute("""
        SELECT f.id, f.folder, f.bytes, f.phone_path, v.kind, v.words, v.people, v.screen, v.ms,
               s.bucket, s.preselected, s.rule
        FROM files f JOIN verdicts v ON v.file_id = f.id LEFT JOIN suggestions s ON s.file_id = f.id
        WHERE f.folder IN (%s)""" % ",".join("?" * len(MODEL_FOLDERS)), MODEL_FOLDERS).fetchall()
    return out


def tally(rs):
    n = len(rs)
    alone = sum(1 for r in rs if guard.model_alone(r))
    ticked = sum(1 for r in rs if r["bucket"] and r["preselected"])
    shown = sum(1 for r in rs if r["bucket"])
    return {"n": n, "bytes": sum(r["bytes"] for r in rs), "alone": alone, "ticked": ticked, "shown": shown,
            "kinds": Counter(r["kind"] or "unreadable" for r in rs),
            "ticked_bytes": sum(r["bytes"] for r in rs if r["bucket"] and r["preselected"])}


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def main(bin_db, phone_db, out="eval/results.md"):
    deleted, kept = rows(bin_db), rows(phone_db)
    d, k = tally(deleted), tally(kept)
    lines = ["# The sweep against a person's own clean-up of the same phone", "",
             "Same phone, same night. The person deleted by hand first; the sweep then looked at the pictures they "
             "deleted (from the gallery's Recently deleted bin) and the ones they kept. Model and rules frozen before "
             "either set was scanned; each set scanned once.", "",
             "| | Pictures | Gemma alone would delete | Gemma + rules, ticked | Gemma + rules, shown at all |",
             "|---|---|---|---|---|",
             f"| Deleted by hand | {d['n']} | {d['alone']} ({pct(d['alone'], d['n'])}) | {d['ticked']} "
             f"({pct(d['ticked'], d['n'])}) | {d['shown']} ({pct(d['shown'], d['n'])}) |",
             f"| Kept by hand | {k['n']} | {k['alone']} ({pct(k['alone'], k['n'])}) | {k['ticked']} "
             f"({pct(k['ticked'], k['n'])}) | {k['shown']} ({pct(k['shown'], k['n'])}) |", "",
             f"Kinds Gemma gave the pictures deleted by hand: {dict(d['kinds'].most_common())}", "",
             f"Kinds Gemma gave the pictures kept: {dict(k['kinds'].most_common())}", "",
             "Among the kept pictures, every ticked suggestion is either junk the person missed or a picture they "
             "want; those are labelled by the person (`sweep label`) and counted in `sweep report`.", ""]
    ms = [r["ms"] for r in deleted + kept if r["ms"]]
    if ms:
        lines.append(f"Gemma's time per picture: {sum(ms) / len(ms) / 1000:.1f} s on average over {len(ms)} answers "
                     f"(laptop CPU, no GPU).")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(*sys.argv[1:3])
