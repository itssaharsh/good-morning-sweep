"""sweep: free a phone's storage of WhatsApp forwards, copies and mistakes, with a local open model and a review.

  sweep devices                 is the phone connected?
  sweep run                     copy the phone's pictures, index them, let Gemma look, compute suggestions
  sweep review                  open the review page (tap to keep; "Delete from phone")
  sweep report                  numbers: found, freed, model alone vs the guard
Steps one by one: pull, index, scan, suggest, apply, label. `sweep import <folder>` uses a folder instead of a phone.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import db, guard, model, pull, report, review, scan
from .apply import apply as apply_fn, NotReviewed
from .phone import Phone, PhoneError, find_adb


def win_home():
    try:
        out = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True, text=True, timeout=20,
                             cwd="/mnt/c").stdout.strip()
        return Path(subprocess.run(["wslpath", "-u", out], capture_output=True, text=True).stdout.strip())
    except Exception:
        return None


def places(args, serial):
    """(workspace for the database and thumbnails, mirror folder for the copied files)."""
    base = Path(args.workspace or os.environ.get("SWEEP_HOME") or Path.home() / "GoodMorningSweep")
    name = args.name or serial
    if not name:  # the workspace used last
        dbs = sorted(base.glob("*/sweep.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        name = dbs[0].parent.name if dbs else "local"
    ws = base / name
    if args.mirror:
        mirror = Path(args.mirror)
    else:
        try:
            exe = str(find_adb()).endswith(".exe")
        except PhoneError:
            exe = False
        wh = win_home() if exe else None
        mirror = (wh / "GoodMorningSweep" / name / "mirror") if wh else ws / "mirror"
    ws.mkdir(parents=True, exist_ok=True)
    return ws, mirror


def open_db(args, serial=None):
    ws, mirror = places(args, serial)
    return db.connect(ws / "sweep.db"), ws, mirror


def phone_or_exit(args):
    ph = Phone(args.serial)
    try:
        d = ph.require_device()
    except PhoneError as e:
        sys.exit(str(e))
    return ph, d


def cmd_devices(args):
    ph = Phone(args.serial)
    devs = ph.devices()
    if not devs:
        print("No phone connected.")
    for d in devs:
        print(d["serial"], d["state"], d["info"])
    if devs and devs[0]["state"] == "device":
        ph.serial = devs[0]["serial"]
        print(ph.model_name(), f"| free {ph.free_bytes() / 1e9:.2f} GB")


def do_pull(args, con, mirror, ph, d):
    db.set_meta(con, "device", ph.model_name())
    db.set_meta(con, "free_at_pull", ph.free_bytes())
    t0 = time.time()
    n, b = pull.pull(con, ph, mirror, only=args.only)
    db.set_meta(con, "pull_seconds", int(time.time() - t0))
    print(f"Copied {n} files ({b / 1e9:.2f} GB) to {mirror} in {int(time.time() - t0)} s. This copy is the backup.")


def cmd_pull(args):
    ph, d = phone_or_exit(args)
    con, ws, mirror = open_db(args, d["serial"])
    do_pull(args, con, mirror, ph, d)
    pull.index(con)


def cmd_import(args):
    """Index a local folder as if it came from a phone: subfolders named like the phone's (wa_images, camera...)."""
    con, ws, mirror = open_db(args, args.name or Path(args.folder).name)
    root = Path(args.folder)
    n = 0
    with con:
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            rel = p.relative_to(root)
            folder = rel.parts[0] if len(rel.parts) > 1 else "wa_images"
            st = p.stat()
            con.execute("INSERT INTO files(phone_path, local_path, folder, bytes, mtime) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(phone_path) DO NOTHING", (f"/import/{rel}", str(p), folder, st.st_size, st.st_mtime))
            n += 1
    db.set_meta(con, "device", args.device or f"the folder {root.name}")
    print(f"Imported {n} files from {root}")
    pull.index(con)


def cmd_index(args):
    con, _, _ = open_db(args, args.serial)
    print(f"Indexed {pull.index(con)} files")


def need_model(url):
    if not model.healthy(url):
        sys.exit(f"Gemma is not running at {url}. Start it with: scripts/start-model.sh")


def cmd_scan(args):
    con, ws, _ = open_db(args, args.serial)
    need_model(args.model_url)
    if args.durable:
        from . import durable
        n = durable.run_sync(ws / "sweep.db", args.model_url, f"sweep-{ws.name}", address=args.temporal)
        print(f"Workflow finished: {n} pictures looked at.")
    else:
        scan.run(con, limit=args.limit, url=args.model_url)
    guard.build(con)
    print(report.summary(con))


def cmd_suggest(args):
    con, _, _ = open_db(args, args.serial)
    out = guard.build(con)
    print(f"{len(out)} suggestions")
    print(report.summary(con))


def cmd_run(args):
    need_model(args.model_url)
    ph, d = phone_or_exit(args)
    con, ws, mirror = open_db(args, d["serial"])
    do_pull(args, con, mirror, ph, d)
    pull.index(con)
    t0 = time.time()
    scan.run(con, url=args.model_url)
    db.set_meta(con, "scan_seconds", int(time.time() - t0))
    guard.build(con)
    print(report.summary(con))
    print("Next: sweep review")


def cmd_review(args):
    con, ws, _ = open_db(args, args.serial)
    if args.static:
        Path(args.static).write_text(review.page(con, static=True, thumb_dir=ws / "thumbs" / "d",
                                                 buckets=review.PUBLIC_BUCKETS), encoding="utf-8")
        print(f"Wrote {args.static}")
        return
    dbpath = ws / "sweep.db"

    def do_apply(c):
        ph = Phone(args.serial)
        ph.require_device()
        return apply_fn(c, ph)
    review.serve(lambda: db.connect(dbpath), ws, do_apply, port=args.port)


def cmd_apply(args):
    con, _, _ = open_db(args, args.serial)
    ph, d = (None, None) if args.dry_run else phone_or_exit(args)
    try:
        print(apply_fn(con, ph, dry_run=args.dry_run))
    except NotReviewed as e:
        sys.exit(str(e))


def cmd_report(args):
    con, _, _ = open_db(args, args.serial)
    print(report.summary(con))
    print()
    print(report.versus_text(report.versus(con)))


def cmd_label(args):
    from . import label
    con, ws, _ = open_db(args, args.serial)
    dbpath = ws / "sweep.db"
    label.serve(lambda: db.connect(dbpath), ws, n=args.n, port=args.port, seed=args.seed)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sweep", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--serial", help="which phone, when more than one is plugged in (also names its workspace)")
    ap.add_argument("--name", help="workspace name (default: the phone's serial)")
    ap.add_argument("--workspace", help="where the database lives (default ~/GoodMorningSweep)")
    ap.add_argument("--mirror", help="where copied files go (default: a folder Windows adb can write to)")
    ap.add_argument("--model-url", default=model.MODEL_URL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices").set_defaults(f=cmd_devices)
    p = sub.add_parser("pull"); p.add_argument("--only", nargs="*"); p.set_defaults(f=cmd_pull)
    p = sub.add_parser("import"); p.add_argument("folder"); p.add_argument("--device"); p.set_defaults(f=cmd_import)
    sub.add_parser("index").set_defaults(f=cmd_index)
    p = sub.add_parser("scan"); p.add_argument("--limit", type=int)
    p.add_argument("--durable", action="store_true", help="run as a Temporal workflow that survives crashes")
    p.add_argument("--temporal", default="localhost:7233"); p.set_defaults(f=cmd_scan)
    sub.add_parser("suggest").set_defaults(f=cmd_suggest)
    p = sub.add_parser("run"); p.add_argument("--only", nargs="*"); p.set_defaults(f=cmd_run)
    p = sub.add_parser("review"); p.add_argument("--port", type=int, default=8765); p.add_argument("--static")
    p.set_defaults(f=cmd_review)
    p = sub.add_parser("apply"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(f=cmd_apply)
    sub.add_parser("report").set_defaults(f=cmd_report)
    p = sub.add_parser("label"); p.add_argument("--n", type=int, default=200); p.add_argument("--port", type=int, default=8766)
    p.add_argument("--seed", type=int, default=7); p.set_defaults(f=cmd_label)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
