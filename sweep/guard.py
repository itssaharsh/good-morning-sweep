"""The guard: plain rules that turn what the model said and what the code measured into suggestions.

The model never deletes anything. It only describes a picture. These rules decide, and each one writes its reason in
words the person reviewing can check against the picture. Anything the rules are unsure about is kept."""
import datetime as dt
import re

from . import checks, media
from .wordlist import document_words, forward_words, has_words

FORWARD_KINDS = {"greeting", "quote", "meme", "news", "ad"}
ARTICLE = {"greeting": "a greeting", "quote": "a quote", "meme": "a meme", "news": "forwarded news", "ad": "an ad"}
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


def _is_video(row):
    return media.is_video(row["phone_path"])


def _single(r, v, c, today, arrivals):
    """The rule for one file on its own: (bucket, reason, rule, keep_id), or None to keep it."""
    kind = v["kind"] if v else None
    words = v["words"] if v else ""
    folder = r["folder"]
    people = v["people"] if v else None

    # Rule K1: no usable answer from the model means keep.
    if not v or not kind:
        return None

    # Rule P1/F1/F2: a WhatsApp forward. Real people in it, or the model not saying, makes it the person's call.
    if kind in FORWARD_KINDS and folder in WA_FOLDERS:
        if people is not False and people != 0:
            return ("check", f"Looks like {ARTICLE.get(kind, kind)}, but may have real people in it" +
                    (f": “{short(words, 40)}”" if words else ""), "P1-people-in-forward", None)
        # Rule D1: anything that reads like a bill, receipt, order, ID, ticket or study question is kept, whatever
        # the model called it.
        if document_words(words):
            return None
        what = "Forwarded video" if folder.startswith("wa_video") else f"Forwarded {kind}"
        # Rule P2: something you sent yourself is your choice, never a forward that arrived: shown, never ticked.
        if folder in {"wa_sent", "wa_video_sent"}:
            if has_words(words) and forward_words(words):
                return ("check", f"You sent this; it looks like {ARTICLE.get(kind, kind)}: “{short(words, 50)}”",
                        "P2-you-sent-it", None)
            return None
        if has_words(words) and forward_words(words):
            return ("forwards", f"{what}. It reads: “{short(words)}”", "F1-read-words", None)
        if arrivals.get(r["id"], 1) >= 2:
            return ("forwards", f"{what}, arrived {arrivals[r['id']]} times", "F2-arrived-again", None)
        return None    # a forward with no forward word that it read, and that came once: kept

    # Rule B1: a blurry or black photo (never a video: one frame says nothing about the rest), flagged by code,
    # confirmed by the model, with no people in it.
    if c and not _is_video(r) and kind == "accidental" and (people is False or people == 0) and \
            (c["blur"] < checks.BLUR_FLAG or c["dark"] < checks.DARK_FLAG):
        how = "black" if c["dark"] < checks.DARK_FLAG else "blurry"
        return ("blurry", f"Looks {how} (sharpness {c['blur']:.0f}, brightness {c['dark']:.0f})",
                "B1-code-and-model", None)

    # Rule S1: old screenshots of one-time codes, payments and deliveries: probably done with. Never ticked.
    if (folder == "screenshots" or kind == "screenshot") and kind not in {"photo", "document"}:
        st = screen_type(v)
        age = (today - (r["mtime"] or today)) / 86400
        if st and age >= OLD_SCREEN_DAYS:
            when = dt.datetime.fromtimestamp(r["mtime"]).strftime("%d %b %Y")
            return ("screenshots", f"{SCREEN_NAMES[st].capitalize()} screenshot from {when}: “{short(words, 50)}”",
                    "S1-old-screenshot", None)
    return None


def decide(rows, verdicts, chk, today=None):
    """rows: file rows; verdicts and chk: {file_id: row}. Returns {file_id: (bucket, reason, rule, keep_id)}."""
    today = today or dt.datetime.now().timestamp()
    out = {}
    sharp = {fid: (c["blur"] or 0.0) for fid, c in chk.items()}

    # How many times each picture arrived (exact copies plus look-alikes that are forwards too).
    exact = checks.exact_groups(rows)
    copies_of = {}
    for group in exact.values():
        for r in group:
            copies_of[r["id"]] = len(group)
    arrivals = {r["id"]: copies_of.get(r["id"], 1) for r in rows}
    near_of = {}
    for g in checks.near_groups(rows):
        fwd = [r for r in g if verdicts.get(r["id"]) and verdicts[r["id"]]["kind"] in FORWARD_KINDS]
        total = sum(copies_of.get(r["id"], 1) for r in fwd)
        for r in g:
            if r in fwd:
                arrivals[r["id"]] = max(arrivals[r["id"]], total)
            near_of[r["id"]] = g

    # Each file on its own.
    single = {r["id"]: _single(r, verdicts.get(r["id"]), chk.get(r["id"]), today, arrivals) for r in rows}

    # Rule C1: exact copies. When the best copy is itself junk (a forward, a black shot), every copy goes with it.
    # Otherwise exactly one copy stays (the original over a WhatsApp copy) and the rest are "copies".
    in_group = set()
    for group in exact.values():
        group = sorted(group, key=lambda r: checks.keeper_key(r, sharp.get(r["id"])))
        keep = group[0]
        mine = single.get(keep["id"])
        if mine and PRESELECT[mine[0]]:
            for r in group:
                b, reason, rule, _ = single.get(r["id"]) or mine
                if not PRESELECT[b]:
                    b, reason, rule = mine[0], mine[1], mine[2]
                out[r["id"]] = (b, f"{reason} (one of {len(group)} identical copies)", rule, None)
        else:
            if mine:
                out[keep["id"]] = mine
            for r in group[1:]:
                out[r["id"]] = ("copies", f"Exact copy of {_name(keep)}, which is kept", "C1-exact-copy", keep["id"])
        in_group.update(r["id"] for r in group)

    for r in rows:
        fid = r["id"]
        if fid in in_group:
            continue
        if single.get(fid):
            out[fid] = single[fid]
            continue
        # Rule N1: a photo that looks almost the same as a sharper one that is kept. Never ticked; never videos.
        g = near_of.get(fid)
        v = verdicts.get(fid)
        kind = v["kind"] if v else None
        if g and kind in {"photo", None} and r["folder"] in {"camera", "wa_images"} and not _is_video(r):
            def photo_like(x):   # only a photo is "the same photo": never a card made from one
                vx = verdicts.get(x["id"])
                return x["folder"] in {"camera", "wa_images"} and not _is_video(x) and \
                    (vx["kind"] if vx else None) in {"photo", None}
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
    rows = con.execute("SELECT * FROM files WHERE sha1 IS NOT NULL AND COALESCE(gone, 0) = 0 AND id NOT IN "
                       "(SELECT file_id FROM deletions WHERE ok = 1)").fetchall()
    verdicts = {r["file_id"]: r for r in con.execute("SELECT * FROM verdicts")}
    chk = {r["file_id"]: r for r in con.execute("SELECT * FROM checks")}
    out = decide(rows, verdicts, chk, today)
    with con:
        con.execute("INSERT INTO meta(key, value) VALUES('suggested_at', ?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value", (str(dt.datetime.now().timestamp()),))
        con.execute("DELETE FROM suggestions")
        con.executemany(
            "INSERT INTO suggestions(file_id, bucket, reason, rule, keep_id, preselected) VALUES(?,?,?,?,?,?)",
            [(fid, b, reason, rule, keep, 1 if PRESELECT[b] else 0) for fid, (b, reason, rule, keep) in out.items()])
    return out
