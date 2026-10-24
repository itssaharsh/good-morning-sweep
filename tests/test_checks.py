import shutil
from sweep import checks, pull
from conftest import card, photo, add


def test_exact_and_near_copies(con, tmp_path):
    a = card(tmp_path / "a.jpg", seed=1)
    b = tmp_path / "b.jpg"; shutil.copy(a, b)
    from PIL import Image
    Image.open(a).save(tmp_path / "c.jpg", quality=60)        # same picture, recompressed
    photo(tmp_path / "d.jpg")
    for name, f in (("a.jpg", "wa_images"), ("b.jpg", "wa_sent"), ("c.jpg", "wa_images"), ("d.jpg", "camera")):
        add(con, tmp_path / name, f)
    pull.index(con)
    rows = con.execute("SELECT * FROM files").fetchall()
    ex = checks.exact_groups(rows)
    assert len(ex) == 1 and len(list(ex.values())[0]) == 2
    near = checks.near_groups(rows)
    assert len(near) == 1 and {r["local_path"].rsplit("/", 1)[-1] for r in near[0]} <= {"a.jpg", "b.jpg", "c.jpg"}


def test_keeper_prefers_camera_original():
    rows = [{"id": 1, "folder": "wa_sent", "width": 100, "height": 100, "mtime": 1},
            {"id": 2, "folder": "camera", "width": 100, "height": 100, "mtime": 2}]
    assert sorted(rows, key=checks.keeper_key)[0]["id"] == 2


def test_blur_and_dark_scores(tmp_path):
    from PIL import Image
    from sweep import media
    black = Image.new("RGB", (200, 200), (3, 3, 3))
    sharp, dark = media.blur_and_dark(black)
    assert dark < checks.DARK_FLAG and sharp < checks.BLUR_FLAG
    s2, d2 = media.blur_and_dark(Image.open(photo(tmp_path / "p.jpg")))
    assert s2 > checks.BLUR_FLAG and d2 > checks.DARK_FLAG
