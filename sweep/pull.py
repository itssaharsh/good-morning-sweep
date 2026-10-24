"""Copy the phone's picture folders to the laptop (this copy is also the backup) and index each file."""
from pathlib import Path

from . import media


def folder_label(label, phone_path, root):
    rel = phone_path[len(root):].lstrip("/")
    if label == "wa_images" and rel.split("/")[0] == "Sent":
        return "wa_sent"
    if label == "wa_video" and rel.split("/")[0] == "Sent":
        return "wa_video_sent"
    return label


def pull(con, phone, mirror, log=print, only=None):
    mirror = Path(mirror)
    total_files = total_bytes = 0
    for label, root in phone.roots():
        if only and label not in only:
            continue
        remote = phone.list_files(root)
        base = mirror / label
        local_root = base / Path(root).name

        def local_of(p):
            return local_root / p[len(root):].lstrip("/")

        missing = [r for r in remote if not (local_of(r[0]).exists() and local_of(r[0]).stat().st_size == r[1])]
        size = sum(r[1] for r in remote)
        log(f"{label:12s} {len(remote):6d} files  {size / 1e9:6.2f} GB  ({len(missing)} to copy)  {root}")
        if missing and (len(missing) > 40 or len(missing) > len(remote) / 2):
            phone.pull_dir(root, base)
        else:
            for p, _, _ in missing:
                phone.pull_file(p, local_of(p))
        with con:
            for p, b, mt in remote:
                con.execute(
                    "INSERT INTO files(phone_path, local_path, folder, bytes, mtime) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(phone_path) DO UPDATE SET local_path=excluded.local_path, bytes=excluded.bytes, "
                    "mtime=excluded.mtime",
                    (p, str(local_of(p)), folder_label(label, p, root), b, mt))
        total_files += len(remote)
        total_bytes += size
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
