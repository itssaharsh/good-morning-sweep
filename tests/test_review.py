from sweep import review, guard
from conftest import add, card


def test_page_renders_and_save_records_every_decision(con, tmp_path):
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    b = add(con, card(tmp_path / "b.jpg", seed=5), "wa_images")
    con.executemany("INSERT INTO suggestions(file_id,bucket,reason,rule,preselected) VALUES(?,?,?,?,?)",
                    [(a, "forwards", "It reads: “Good Morning”", "F1", 1), (b, "screenshots", "old", "S1", 0)])
    con.commit()
    html = review.page(con)
    assert "Good Morning Sweep" in html and "It reads" in html
    assert review.save(con, [a]) == 1
    acts = dict(con.execute("SELECT file_id, action FROM decisions").fetchall())
    assert acts == {a: "delete", b: "keep"}


def test_static_page_embeds_thumbnails(con, tmp_path):
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    con.execute("INSERT INTO suggestions(file_id,bucket,reason,rule,preselected) VALUES(?,?,?,?,?)", (a, "forwards", "r", "F1", 1))
    con.commit()
    html = review.page(con, static=True, thumb_dir=tmp_path / "t")
    assert "data:image/jpeg;base64," in html and "const STATIC = true" in html


def test_static_demo_never_includes_family_buckets(con, tmp_path):
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    b = add(con, card(tmp_path / "b.jpg", seed=9), "camera")
    con.executemany("INSERT INTO suggestions(file_id,bucket,reason,rule,preselected) VALUES(?,?,?,?,?)",
                    [(a, "forwards", "fwd", "F1", 1), (b, "similar", "a family photo", "N1", 0)])
    con.commit()
    html = review.page(con, static=True, thumb_dir=tmp_path / "t", buckets=review.PUBLIC_BUCKETS)
    assert "fwd" in html and "a family photo" not in html
