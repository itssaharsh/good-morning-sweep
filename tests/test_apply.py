import pytest
from sweep import review
from sweep.apply import apply, NotReviewed
from conftest import add, card


class FakePhone:
    def __init__(self):
        self.deleted, self.free = [], 1_000_000_000

    def free_bytes(self):
        return self.free

    def delete(self, paths):
        self.deleted += paths
        self.free += 1000 * len(paths)
        return {p: (True, "deleted") for p in paths}


def setup(con, tmp_path):
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    b = add(con, card(tmp_path / "b.jpg", seed=3), "wa_images")
    con.executemany("INSERT INTO suggestions(file_id,bucket,reason,rule,preselected) VALUES(?,?,?,?,?)",
                    [(a, "forwards", "r", "F1", 1), (b, "forwards", "r", "F1", 1)])
    con.commit()
    return a, b


def test_refuses_without_a_saved_review(con, tmp_path):
    setup(con, tmp_path)
    with pytest.raises(NotReviewed):
        apply(con, FakePhone(), log=lambda *a: None)


def test_deletes_only_what_was_approved_and_reports_bytes(con, tmp_path):
    a, b = setup(con, tmp_path)
    review.save(con, [a])
    ph = FakePhone()
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 1 and len(ph.deleted) == 1 and ph.deleted[0].endswith("a.jpg")
    assert res["freed"] == (tmp_path / "a.jpg").stat().st_size
    again = apply(con, ph, log=lambda *a: None)          # a second run deletes nothing new
    assert again["deleted"] == 0


def test_never_deletes_a_file_whose_laptop_copy_is_missing(con, tmp_path):
    a, b = setup(con, tmp_path)
    review.save(con, [a, b])
    (tmp_path / "b.jpg").unlink()
    ph = FakePhone()
    res = apply(con, ph, log=lambda *a: None)
    assert res["skipped"] == 1 and all(not p.endswith("b.jpg") for p in ph.deleted)
