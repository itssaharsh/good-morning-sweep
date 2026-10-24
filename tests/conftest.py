import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sweep import db  # noqa: E402


def card(path, text="Good Morning", seed=1, size=(480, 480)):
    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 200, 90))
    d = ImageDraw.Draw(im)
    for _ in range(30):
        x, y, r = rng.randint(0, size[0]), rng.randint(0, size[1]), rng.randint(8, 40)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(rng.randint(150, 255), rng.randint(40, 160), 120))
    d.text((60, 220), text, fill=(120, 0, 40))
    im.save(path, quality=90)
    return path


def photo(path, seed=2, size=(640, 480)):
    rng = random.Random(seed)
    im = Image.new("RGB", size)
    px = im.load()
    for x in range(0, size[0], 4):
        for y in range(0, size[1], 4):
            c = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            for dx in range(4):
                for dy in range(4):
                    px[x + dx, y + dy] = c
    im.save(path, quality=92)
    return path


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "sweep.db")
    yield c
    c.close()


def add(con, local, folder, phone_path=None, mtime=1_700_000_000):
    p = Path(local)
    cur = con.execute("INSERT INTO files(phone_path, local_path, folder, bytes, mtime) VALUES(?,?,?,?,?)",
                      (phone_path or f"/sdcard/x/{folder}/{p.name}", str(p), folder, p.stat().st_size, mtime))
    con.commit()
    return cur.lastrowid
