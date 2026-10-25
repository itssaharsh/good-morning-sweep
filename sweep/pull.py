"""Copy the phone's picture folders to the laptop (this copy is also the backup) and index each file."""
import datetime as dt
import re
import shutil
from pathlib import Path

from . import media


MARGIN = 5 * 10**9   # always leave this much free on the laptop
SKIP = {"Private", ".Statuses", ".trash", ".thumbnails"}


def folder_label(label, phone_path, root):
    rel = phone_path[len(root):].lstrip("/")
    if label == "wa_images" and rel.split("/")[0] == "Sent":
        return "wa_sent"
    if label == "wa_video" and rel.split("/")[0] == "Sent":
        return "wa_video_sent"
    return label


def slug(path):
    """A folder name for the phone folder's parent, so two phone folders with the same name never share a copy."""
    return "_".join(x for x in str(path).strip("/").split("/") if x) or "root"


def pull(con, phone, mirror, log=print, only=None):
    mirror = Path(mirror)
    total_files = total_bytes = 0
    for label, root in phone.roots():
        if only and label not in only:
            continue
        remote = phone.list_files(root)
        base = mirror / label / slug(Path(root).parent)
        local_root = base / Path(root).name

        def local_of(p):
            return local_root / p[len(root):].lstrip("/")

        missing = [r for r in remote if not (local_of(r[0]).exists() and local_of(r[0]).stat().st_size == r[1])]
        size = sum(r[1] for r in remote)
        need = sum(r[1] for r in missing)
        log(f"{label:12s} {len(remote):6d} files  {size / 1e9:6.2f} GB  ({len(missing)} to copy)  {root}")
        mirror.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(mirror).free
        if need > free - MARGIN:
            log(f"  skipped: copying it needs {need / 1e9:.1f} GB and the laptop has {free / 1e9:.1f} GB free "
                f"(keeping {MARGIN / 1e9:.0f} GB spare). Free some space, or run with --only to pick folders.")
            continue
        failed = []
        if missing and (len(missing) > 40 or len(missing) > len(remote) / 2):
            try:
                phone.pull_dir(root, base)
            except Exception as e:   # one bad file stops adb's folder copy: copy the rest one by one
                log(f"  folder copy stopped ({str(e)[:120]}); copying the missing files one by one")
                missing = [r for r in missing
                           if not (local_of(r[0]).exists() and local_of(r[0]).stat().st_size == r[1])]
                for p, _, _ in missing:
                    try:
                        phone.pull_file(p, local_of(p))
                    except Exception:
                        failed.append(p)
        else:
            for p, _, _ in missing:
                try:
                    phone.pull_file(p, local_of(p))
                except Exception:
                    failed.append(p)
        if failed:
            log(f"  {len(failed)} files could not be copied; they are left out and stay on the phone")
        listed = set()
        with con:
            for p, b, mt in remote:
                lp = local_of(p)
                if p in failed or not (lp.exists() and lp.stat().st_size == b):
                    continue
                listed.add(p)
                old = con.execute("SELECT id, bytes, mtime FROM files WHERE phone_path=?", (p,)).fetchone()
                if old and (old["bytes"] != b or (old["mtime"] or 0) != mt):
                    # the file changed on the phone: everything known about the old one is forgotten
                    for t in ("verdicts", "checks", "suggestions", "decisions", "labels"):
                        con.execute(f"DELETE FROM {t} WHERE file_id=?", (old["id"],))
                    con.execute("UPDATE files SET sha1=NULL, dhash=NULL, width=NULL, height=NULL WHERE id=?",
                                (old["id"],))
                con.execute(
                    "INSERT INTO files(phone_path, local_path, folder, bytes, mtime, root, gone) VALUES(?,?,?,?,?,?,0) "
                    "ON CONFLICT(phone_path) DO UPDATE SET local_path=excluded.local_path, bytes=excluded.bytes, "
                    "mtime=excluded.mtime, root=excluded.root, gone=0",
                    (p, str(lp), folder_label(label, p, root), b, mt, root))
            # files this folder no longer has (deleted on the phone since the last copy)
            for r in con.execute("SELECT id, phone_path FROM files WHERE root=? AND gone=0", (root,)).fetchall():
                if r["phone_path"] not in listed and r["phone_path"] not in failed:
                    con.execute("UPDATE files SET gone=1 WHERE id=?", (r["id"],))
        total_files += len(listed)
        total_bytes += sum(b for p, b, _ in remote if p in listed)
    return total_files, total_bytes


def index(con, log=print, every=500):
    """sha1, size in pixels, perceptual hash, sharpness and brightness for each copied file not indexed yet."""
    rows = con.execute("SELECT id, local_path FROM files WHERE sha1 IS NULL").fetchall()
    for n, r in enumerate(rows, 1):
        p = r["local_path"]
        if not Path(p).exists():
            continue
        h = media.sha1(p)
        im = media.open_picture(p, max_side=512)
        if im is not None:
            from PIL import Image as _I
            w, hgt = _I.open(p).size if not media.is_video(p) else (im.width, im.height)
            dh = media.dhash(im)
            blur, dark = media.blur_and_dark(im)
            con.execute("INSERT INTO checks(file_id, blur, dark) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET "
                        "blur=excluded.blur, dark=excluded.dark", (r["id"], blur, dark))
        else:
            dh, w, hgt = None, None, None
        con.execute("UPDATE files SET sha1=?, dhash=?, width=?, height=? WHERE id=?", (h, dh, w, hgt, r["id"]))
        if n % every == 0:
            con.commit()
            log(f"indexed {n}/{len(rows)}")
    con.commit()
    return len(rows)


# A copy made in file-transfer (MTP) mode, without USB debugging: folder name on the laptop -> folder on the phone.
MTP_ROOTS = {
    "wa_images": ("wa_images", "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"),
    "wa_video": ("wa_video", "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video"),
    "screenshots_dcim": ("screenshots", "/sdcard/DCIM/Screenshots"),
    "screenshots_pictures": ("screenshots", "/sdcard/Pictures/Screenshots"),
    "camera": ("camera", "/sdcard/DCIM/Camera"),
}
_DATE = re.compile(r"(20\d{2})(\d{2})(\d{2})(?:[-_](\d{2})(\d{2})(\d{2}))?")


def date_from_name(name, fallback):
    """WhatsApp and screenshot names carry their date (IMG-20261012-WA0003, Screenshot_20260801-101010)."""
    m = _DATE.search(name)
    if not m:
        return fallback
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm, ss = (int(m.group(i) or 0) for i in (4, 5, 6))
        return dt.datetime(y, mo, d, hh, mm, ss).timestamp()
    except ValueError:
        return fallback


def import_mtp(con, mirror, log=print):
    """Index a folder copied from the phone in file-transfer mode (mirror/<label>/<phone folder name>/...).
    Phone paths are rebuilt from the layout, so a later delete over adb finds each file (and checks its SHA-1)."""
    mirror = Path(mirror)
    n = b = 0
    with con:
        for key, (label, root) in MTP_ROOTS.items():
            top = mirror / key / Path(root).name
            if not top.is_dir():
                continue
            for f in sorted(top.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(top).as_posix()
                if any(part in SKIP or part.startswith(".") for part in rel.split("/")):
                    continue
                phone_path = f"{root}/{rel}"
                st = f.stat()
                con.execute(
                    "INSERT INTO files(phone_path, local_path, folder, bytes, mtime, root, gone) VALUES(?,?,?,?,?,?,0) "
                    "ON CONFLICT(phone_path) DO UPDATE SET local_path=excluded.local_path, bytes=excluded.bytes, "
                    "mtime=excluded.mtime, root=excluded.root, gone=0",
                    (phone_path, str(f), folder_label(label, phone_path, root), st.st_size,
                     date_from_name(f.name, st.st_mtime), root))
                n += 1
                b += st.st_size
            log(f"{key:20s} imported from {top}")
    return n, b
