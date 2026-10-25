from sweep.phone import Phone, PhoneError
from sweep import pull
import pytest


class FakeAdb:
    def __init__(self, files, devices="List of devices attached\nABC123\tdevice model:Moto\n"):
        self.files, self.dev, self.calls = files, devices, []

    def __call__(self, args, timeout=0):
        self.calls.append(args)
        if args[:1] == ["devices"]:
            return 0, self.dev, ""
        if args[:1] == ["shell"]:
            c = args[1]
            if c.startswith("[ -d"):
                return 0, ("yes" if any(f.startswith(c.split("'")[1] if "'" in c else c.split()[2]) for f in self.files) else "no"), ""
            if c.startswith("find"):
                root = c.split("'")[1] if "'" in c else c.split()[1]
                return 0, "\n".join(f"{s}|1700000000|{p}" for p, s in self.files.items() if p.startswith(root)), ""
            if c.startswith("getprop"):
                return 0, "Moto G\n", ""
            if c.startswith("df"):
                return 0, "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/x 100 50 2000000 50% /data\n", ""
        if args[:1] == ["pull"]:
            return 0, "pulled", ""
        return 1, "", "unknown"


def test_unauthorized_phone_says_tap_allow():
    ph = Phone(runner=FakeAdb({}, devices="List of devices attached\nABC\tunauthorized\n"))
    with pytest.raises(PhoneError, match="Allow"):
        ph.require_device()


def test_no_phone_says_how_to_connect():
    ph = Phone(runner=FakeAdb({}, devices="List of devices attached\n\n"))
    with pytest.raises(PhoneError, match="USB debugging"):
        ph.require_device()


def test_lists_whatsapp_and_skips_private_and_hidden():
    root = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    files = {f"{root}/IMG-1.jpg": 100, f"{root}/Sent/IMG-2.jpg": 50, f"{root}/Private/IMG-3.jpg": 70,
             f"{root}/.nomedia": 0}
    ph = Phone(runner=FakeAdb(files))
    rows = ph.list_files(root)
    names = sorted(p.rsplit("/", 1)[-1] for p, _, _ in rows)
    assert names == ["IMG-1.jpg", "IMG-2.jpg"]


def test_sent_folder_is_labelled_separately():
    root = "/r/WhatsApp Images"
    assert pull.folder_label("wa_images", f"{root}/Sent/a.jpg", root) == "wa_sent"
    assert pull.folder_label("wa_images", f"{root}/a.jpg", root) == "wa_images"


def test_free_bytes_parses_df():
    ph = Phone(runner=FakeAdb({}))
    assert ph.free_bytes() == 2000000 * 1024


class FolderPhone:
    """A fake phone whose folders are real local folders; pull copies files like adb does."""
    def __init__(self, tree):
        self.tree = tree          # {phone path: local source file}

    def roots(self):
        rs = sorted({p.rsplit("/", 1)[0] for p in self.tree})
        return [("screenshots", r) for r in rs]

    def list_files(self, root):
        import os
        return [(p, os.path.getsize(src), 1700000000.0) for p, src in self.tree.items() if p.rsplit("/", 1)[0] == root]

    def pull_dir(self, root, base):
        import shutil
        from pathlib import Path
        for p, src in self.tree.items():
            if p.rsplit("/", 1)[0] == root:
                d = Path(base) / Path(root).name / p.rsplit("/", 1)[1]
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, d)

    def pull_file(self, p, dest):
        import shutil
        from pathlib import Path
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.tree[p], dest)


def test_two_screenshot_folders_never_share_a_laptop_copy(con, tmp_path):
    """/sdcard/DCIM/Screenshots and /sdcard/Pictures/Screenshots used to map to one folder (review finding B4)."""
    from conftest import card
    a, b = card(tmp_path / "a.png", seed=1), card(tmp_path / "b.png", seed=2)
    ph = FolderPhone({"/sdcard/DCIM/Screenshots/s.png": a, "/sdcard/Pictures/Screenshots/s.png": b})
    pull.pull(con, ph, tmp_path / "mirror", log=lambda *x: None)
    pull.index(con)
    rows = con.execute("SELECT local_path, sha1 FROM files").fetchall()
    assert len({r["local_path"] for r in rows}) == 2 and len({r["sha1"] for r in rows}) == 2


def test_a_file_that_changed_on_the_phone_is_checked_again(con, tmp_path):
    from conftest import card
    a = card(tmp_path / "a.png", seed=1)
    ph = FolderPhone({"/sdcard/DCIM/Screenshots/s.png": a})
    pull.pull(con, ph, tmp_path / "mirror", log=lambda *x: None)
    pull.index(con)
    fid = con.execute("SELECT id FROM files").fetchone()[0]
    con.execute("INSERT INTO verdicts(file_id, kind) VALUES(?, 'greeting')", (fid,)); con.commit()
    card(tmp_path / "a.png", seed=9, size=(500, 500))          # new content, new size
    pull.pull(con, ph, tmp_path / "mirror", log=lambda *x: None)
    row = con.execute("SELECT sha1 FROM files WHERE id=?", (fid,)).fetchone()
    assert row["sha1"] is None and con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_files_gone_from_the_phone_are_marked_gone(con, tmp_path):
    from conftest import card
    a, b = card(tmp_path / "a.png", seed=1), card(tmp_path / "b.png", seed=2)
    tree = {"/sdcard/DCIM/Screenshots/a.png": a, "/sdcard/DCIM/Screenshots/b.png": b}
    ph = FolderPhone(tree)
    pull.pull(con, ph, tmp_path / "mirror", log=lambda *x: None)
    del tree["/sdcard/DCIM/Screenshots/b.png"]
    pull.pull(con, ph, tmp_path / "mirror", log=lambda *x: None)
    gone = dict(con.execute("SELECT phone_path, gone FROM files").fetchall())
    assert gone == {"/sdcard/DCIM/Screenshots/a.png": 0, "/sdcard/DCIM/Screenshots/b.png": 1}


def test_a_file_transfer_copy_maps_back_to_phone_paths(con, tmp_path):
    from conftest import card
    m = tmp_path / "mtp"
    (m / "wa_images" / "WhatsApp Images" / "Sent").mkdir(parents=True)
    (m / "wa_images" / "WhatsApp Images" / "Private").mkdir(parents=True)
    (m / "screenshots_dcim" / "Screenshots").mkdir(parents=True)
    card(m / "wa_images" / "WhatsApp Images" / "IMG-20261012-WA0003.jpg")
    card(m / "wa_images" / "WhatsApp Images" / "Sent" / "IMG-20261013-WA0001.jpg", seed=3)
    card(m / "wa_images" / "WhatsApp Images" / "Private" / "IMG-1.jpg", seed=4)
    card(m / "screenshots_dcim" / "Screenshots" / "Screenshot_20260801-101010.png", seed=5)
    n, _ = pull.import_mtp(con, m, log=lambda *a: None)
    rows = {r["phone_path"]: (r["folder"], r["mtime"]) for r in con.execute("SELECT * FROM files")}
    assert n == 3
    wa = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    assert rows[f"{wa}/IMG-20261012-WA0003.jpg"][0] == "wa_images"
    assert rows[f"{wa}/Sent/IMG-20261013-WA0001.jpg"][0] == "wa_sent"
    import datetime as dt
    assert dt.datetime.fromtimestamp(rows["/sdcard/DCIM/Screenshots/Screenshot_20260801-101010.png"][1]).month == 8


def test_the_sha1sum_probe_matches_python():
    import hashlib
    from sweep.phone import KNOWN_SHA1
    assert hashlib.sha1(b"sweep").hexdigest() == KNOWN_SHA1
    ph = Phone(runner=lambda args, timeout=0: (0, KNOWN_SHA1 + "  -\n", ""))
    assert ph.can_hash()
    ph2 = Phone(runner=lambda args, timeout=0: (0, "NOSHA\n", ""))
    assert not ph2.can_hash()
