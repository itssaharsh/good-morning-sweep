"""The post's main picture: how many pictures each approach would delete, on the pictures I kept and on the ones I
deleted by hand. Gemma alone deletes anything it calls a forward or a mistake; Gemma + rules is what gets ticked.

    python eval/chart_versus.py BIN_DB PHONE_DB OUT.png
"""
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
FOLDERS = ("wa_images", "wa_sent", "wa_video", "wa_video_sent", "screenshots")
SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
ALONE, RULES = "#eb6834", "#2a78d6"     # reference palette slots 2 and 1


def counts(db):
    con = sqlite3.connect(db)
    q = ("SELECT COUNT(*), SUM(v.kind IN ('greeting','quote','meme','news','ad','accidental')), "
         "SUM(COALESCE(s.preselected,0)=1) FROM files f JOIN verdicts v ON v.file_id=f.id "
         "LEFT JOIN suggestions s ON s.file_id=f.id WHERE f.folder IN (%s)" % ",".join("?" * len(FOLDERS)))
    n, alone, ticked = con.execute(q, FOLDERS).fetchone()
    return n, alone or 0, ticked or 0


def main(bin_db, phone_db, out):
    rows = [("Pictures I kept", counts(phone_db)), ("Pictures I had deleted by hand\n(random sample)", counts(bin_db))]
    fig, ax = plt.subplots(figsize=(10, 3.6), dpi=200)
    fig.patch.set_facecolor(SURFACE); ax.set_facecolor(SURFACE)
    top = max(max(a, t) for _, (n, a, t) in rows)
    for i, (label, (n, a, t)) in enumerate(rows):
        y = i * 1.0
        for dy, val, color, name in ((-0.17, a, ALONE, "Gemma alone"), (0.17, t, RULES, "Gemma + rules")):
            ax.barh(y + dy, max(val, top * 0.004), height=0.3, color=color, linewidth=0)
            ax.text(val + top * 0.012, y + dy, f"{name}: {val:,}", va="center", fontsize=9.5, color=INK)
        ax.text(-top * 0.02, y, f"{label}\n{n:,} pictures", ha="right", va="center", fontsize=10, color=INK)
    ax.set_xlim(0, top * 1.45); ax.set_ylim(-0.55, 1.55); ax.invert_yaxis(); ax.axis("off")
    fig.text(0.01, 0.95, "Pictures that would be deleted from my phone", fontsize=12.5, color=INK, weight="bold",
             va="top")
    fig.text(0.01, 0.86, "Gemma alone deletes anything it calls a forward or a mistake. Gemma + rules is what the page "
             "ticks.", fontsize=9, color=INK2, va="top")
    fig.subplots_adjust(left=0.27, right=0.97, top=0.78, bottom=0.05)
    fig.savefig(out, facecolor=SURFACE)
    print("wrote", out, rows)


if __name__ == "__main__":
    main(*sys.argv[1:4])
