"""The guard: plain rules that turn what the model said and what the code measured into suggestions.

The model never deletes anything. It only describes a picture. These rules decide, and each one writes its reason in
words the person reviewing can check against the picture. Anything the rules are unsure about is kept."""
import datetime as dt
import re

from . import checks
from .wordlist import forward_words, has_words

FORWARD_KINDS = {"greeting", "quote", "meme", "news", "ad"}
WA_FOLDERS = {"wa_images", "wa_sent", "wa_video", "wa_video_sent"}
OLD_SCREEN_DAYS = 30
SCREEN_NAMES = {"otp": "one-time code", "payment": "payment", "delivery": "delivery tracking"}

BUCKETS = [
    # (bucket, title shown on the review page, ticked for deletion when the review opens)
    ("forwards", "WhatsApp forwards", True),
    ("copies", "Exact copies", True),
    ("blurry", "Blurry or black shots", True),
    ("similar", "Almost the same photo", False),
    ("screenshots", "Old screenshots", False),
    ("check", "Your call", False),
]
PRESELECT = {b: p for b, _, p in BUCKETS}


SCREEN_WORDS = [
    ("otp", re.compile(r"\botp\b|one[- ]time|verification code|\bcode is\b|do not share", re.I)),
    ("payment", re.compile(r"payment|paid|upi|₹|\brs\.?\s?\d|transaction|successful|debited|credited", re.I)),
    ("delivery", re.compile(r"deliver|package|shipped|out for delivery|order|arriving|tracking", re.I)),
]


def screen_type(v):
    """The model's screenshot type, else one read off the words it quoted."""
    if v["screen"] in SCREEN_NAMES:
        return v["screen"]
    for name, rx in SCREEN_WORDS:
        if rx.search(v["words"] or ""):
            return name
    return ""


def short(words, n=70):
    w = " ".join((words or "").split())
    return w if len(w) <= n else w[: n - 1] + "…"


def _name(row):
    return row["phone_path"].rsplit("/", 1)[-1]


def model_alone(v):
    """What the model by itself would throw away: any picture it calls a forward or a mistake (the baseline)."""
    return bool(v) and v["kind"] in FORWARD_KINDS | {"accidental"}


def decide(rows, verdicts, chk, today=None):
    """rows: file rows; verdicts and chk: {file_id: row}. Returns {file_id: (bucket, reason, rule, keep_id)}."""
    today = today or dt.datetime.now().timestamp()
    out = {}
    by_id = {r["id"]: r for r in rows}
    sharp = {fid: (c["blur"] or 0.0) for fid, c in chk.items()}

    # Rule C1: exact copies. One copy stays (the original over a WhatsApp copy); the rest go.
    exact = checks.exact_groups(rows)
    copies_of = {}
    for group in exact.values():
        group = sorted(group, key=lambda r: checks.keeper_key(r, sharp.get(r["id"])))
        keep = group[0]
        for r in group[1:]:
            out[r["id"]] = ("copies", f"Exact copy of {_name(keep)}, which is kept", "C1-exact-copy", keep["id"])
        copies_of[keep["id"]] = len(group)

    # How many times each picture arrived (exact copies plus look-alikes): forwards come in again and again.
    arrivals = {r["id"]: copies_of.get(r["id"], 1) for r in rows}
    near = checks.near_groups(rows)
    near_of = {}
    for g in near:
        fwd = [r for r in g if verdicts.get(r["id"]) and verdicts[r["id"]]["kind"] in FORWARD_KINDS]
        total = sum(copies_of.get(r["id"], 1) for r in fwd)
        for r in g:
            if r in fwd:
                arrivals[r["id"]] = max(arrivals[r["id"]], total)
            near_of[r["id"]] = g

    for r in rows:
        fid = r["id"]
        if fid in out:
            continue
        v, c = verdicts.get(fid), chk.get(fid)
        kind = v["kind"] if v else None
        words = v["words"] if v else ""
        folder = r["folder"]

        # Rule K1: no usable answer from the model means keep.
        if folder != "camera" and (not v or not kind):
            continue

        # Rule F1/F2: a WhatsApp forward is set aside only if the model read its words, or it arrived again and again.
        if kind in FORWARD_KINDS and folder in WA_FOLDERS:
            if v["people"] and kind in {"greeting", "quote"}:
                out[fid] = ("check", f"Looks like a {kind}, but has real people in it" +
                            (f": “{short(words, 40)}”" if words else ""), "P1-people-in-greeting", None)
                continue
            fw = forward_words(words)
            what = "Forwarded video" if folder.startswith("wa_video") else f"Forwarded {kind}"
            if has_words(words) and (fw or kind in {"quote", "news", "ad", "meme"}):
                out[fid] = ("forwards", f"{what}. It reads: “{short(words)}”", "F1-read-words", None)
            elif arrivals.get(fid, 1) >= 2:
                out[fid] = ("forwards", f"{what}, arrived {arrivals[fid]} times", "F2-arrived-again", None)
            continue    # a forward it could not read, that came once: kept

        # Rule B1: a blurry or black shot, flagged by code and confirmed by the model.
        if c and (c["blur"] < checks.BLUR_FLAG or c["dark"] < checks.DARK_FLAG) and kind == "accidental":
            how = "black" if c["dark"] < checks.DARK_FLAG else "blurry"
            out[fid] = ("blurry", f"Looks {how} (sharpness {c['blur']:.0f}, brightness {c['dark']:.0f})",
                        "B1-code-and-model", None)
            continue

        # Rule S1: old screenshots of one-time codes, payments and deliveries: probably done with. Never ticked.
        if (folder == "screenshots" or kind == "screenshot") and v and kind not in {"photo", "document"}:
            st = screen_type(v)
            age = (today - (r["mtime"] or today)) / 86400
            if st and age >= OLD_SCREEN_DAYS:
                when = dt.datetime.fromtimestamp(r["mtime"]).strftime("%d %b %Y")
                out[fid] = ("screenshots", f"{SCREEN_NAMES[st].capitalize()} screenshot from {when}: “{short(words, 50)}”",
                            "S1-old-screenshot", None)
            continue

        # Rule N1: a photo that looks almost the same as a sharper one that is kept. Never ticked.
        g = near_of.get(fid)
        if g and kind in {"photo", None} and folder in {"camera", "wa_images"}:
            def photo_like(x):   # only a photo is "the same photo": never a card made from one
                vx = verdicts.get(x["id"])
                return x["folder"] in {"camera", "wa_images"} and (vx["kind"] if vx else None) in {"photo", None}
            peers = [x for x in g if photo_like(x)]
            if len(peers) < 2:
                continue
            best = sorted(peers, key=lambda x: checks.keeper_key(x, sharp.get(x["id"])))[0]
            if best["id"] != fid and best["id"] not in out:
                out[fid] = ("similar", f"Looks almost the same as {_name(best)}, which is kept", "N1-near-copy",
                            best["id"])
        # Everything else (photos, documents, other screenshots, anything unsure) is kept.
    return out


def build(con, today=None):
    """Recompute every suggestion from the database. Decisions already made are left alone."""
    rows = con.execute("SELECT * FROM files WHERE sha1 IS NOT NULL").fetchall()
    verdicts = {r["file_id"]: r for r in con.execute("SELECT * FROM verdicts")}
    chk = {r["file_id"]: r for r in con.execute("SELECT * FROM checks")}
    out = decide(rows, verdicts, chk, today)
    with con:
        con.execute("DELETE FROM suggestions")
        con.executemany(
            "INSERT INTO suggestions(file_id, bucket, reason, rule, keep_id, preselected) VALUES(?,?,?,?,?,?)",
            [(fid, b, reason, rule, keep, 1 if PRESELECT[b] else 0) for fid, (b, reason, rule, keep) in out.items()])
    return out
