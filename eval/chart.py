"""One picture for the post: what the sweep does with the pictures I deleted by hand, and with the ones I kept.

    python eval/chart.py BIN_DB PHONE_DB OUT.png
"""
import sqlite3
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

FOLDERS = ("wa_images", "wa_sent", "wa_video", "wa_video_sent", "screenshots")
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e7e6e2"
TICKED, SHOWN, KEPT = "#eb6834", "#2a78d6", "#d6d5d0"   # categorical slots 2 and 1 of the reference palette; neutral


def split(db):
    con = sqlite3.connect(db)
    q = ("SELECT SUM(s.preselected=1), SUM(s.preselected=0), SUM(s.file_id IS NULL) FROM files f "
         "JOIN verdicts v ON v.file_id=f.id LEFT JOIN suggestions s ON s.file_id=f.id WHERE f.folder IN (%s)"
         % ",".join("?" * len(FOLDERS)))
    t, sh, k = con.execute(q, FOLDERS).fetchone()
    return [t or 0, sh or 0, k or 0]


def main(bin_db, phone_db, out):
    rows = [("Pictures I deleted by hand\n(random sample)", split(bin_db)), ("Pictures I kept", split(phone_db))]
    fig, ax = plt.subplots(figsize=(10, 3.4), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for y, (label, (t, sh, k)) in enumerate(rows):
        n = t + sh + k
        left = 0
        for val, color, name in ((t, TICKED, "ticked to delete"), (sh, SHOWN, "shown, not ticked"),
                                 (k, KEPT, "kept")):
            w = 100 * val / n if n else 0
            if w:
                ax.barh(y, w - 0.4, left=left + 0.2, height=0.56, color=color, edgecolor=SURFACE, linewidth=0)
                if w >= 7:
                    ax.text(left + w / 2, y, f"{val:,}\n{w:.0f}%", ha="center", va="center", fontsize=9,
                            color="#ffffff" if color != KEPT else INK)
            left += w
        ax.text(-1.5, y, label, ha="right", va="center", fontsize=10, color=INK)
        ax.text(101.5, y, f"{n:,}", ha="left", va="center", fontsize=9, color=INK2)
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.6, 1.6)
    ax.invert_yaxis()
    ax.axis("off")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in (TICKED, SHOWN, KEPT)]
    ax.legend(handles, ["Gemma + rules: ticked to delete", "shown for my call, not ticked", "kept, never suggested"],
              loc="upper center", bbox_to_anchor=(0.5, 1.28), ncol=3, frameon=False, fontsize=9,
              labelcolor=INK2)
    fig.text(0.01, 0.97, "What the sweep does with the same phone, after my 35 minutes by hand", fontsize=12,
             color=INK, weight="bold", va="top")
    fig.subplots_adjust(left=0.25, right=0.93, top=0.72, bottom=0.08)
    fig.savefig(out, facecolor=SURFACE)
    print("wrote", out, rows)


if __name__ == "__main__":
    main(*sys.argv[1:4])
