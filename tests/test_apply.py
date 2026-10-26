import time

import pytest

from sweep import pull, review
from sweep.apply import apply, NotReviewed, WrongPhone
from sweep.db import set_meta
from conftest import add, card


class FakePhone:
    """Deletes from a dict standing in for the phone; checks each file's sha1 the way the phone script does."""
    def __init__(self, files, serial="PHONE1"):
        self.files, self.serial, self.free, self.deleted = dict(files), serial, 1_000_000_000, []

    def free_bytes(self):
        return self.free

    def present(self, paths, scratch_dir):
        return {p for p in paths if p in self.files}

    def delete(self, items, scratch_dir, on_batch=None):
        res = {}
        for p, h in items:
            if p not in self.files:
                res[p] = (False, "not on the phone any more")
            elif self.files[p] != h:
                res[p] = (False, "the phone's copy differs from the backup, so it was kept")
            else:
                del self.files[p]
                self.deleted.append(p)
                self.free += 1000
                res[p] = (True, "deleted")
        if on_batch:
            on_batch(res)
        return res


def setup(con, tmp_path, n=2):
    ids = [add(con, card(tmp_path / f"c{i}.jpg", seed=i + 3), "wa_images") for i in range(n)]
    pull.index(con)
    con.executemany("INSERT INTO suggestions(file_id,bucket,reason,rule,preselected) VALUES(?,?,?,?,?)",
                    [(i, "forwards", "r", "F1", 1) for i in ids])
    con.commit()
    set_meta(con, "serial", "PHONE1")
    files = {r["phone_path"]: r["sha1"] for r in con.execute("SELECT phone_path, sha1 FROM files")}
    return ids, FakePhone(files)


def test_refuses_without_a_saved_review(con, tmp_path):
    _, ph = setup(con, tmp_path)
    with pytest.raises(NotReviewed):
        apply(con, ph, log=lambda *a: None)


def test_deletes_only_what_was_approved_and_reports_bytes(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a])
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 1 and len(ph.deleted) == 1 and ph.deleted[0].endswith("c0.jpg")
    assert res["freed"] == (tmp_path / "c0.jpg").stat().st_size
    assert apply(con, ph, log=lambda *a: None)["deleted"] == 0       # a second run deletes nothing new


def test_refuses_on_a_different_phone(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a, b])
    ph.serial = "SOMEONE-ELSES"
    with pytest.raises(WrongPhone):
        apply(con, ph, log=lambda *a: None)
    assert ph.deleted == []


def test_refuses_when_suggestions_changed_after_the_review(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a])
    set_meta(con, "suggested_at", time.time() + 5)
    with pytest.raises(NotReviewed, match="changed"):
        apply(con, ph, log=lambda *a: None)


def test_never_deletes_when_the_laptop_copy_is_missing_or_different(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a, b])
    (tmp_path / "c0.jpg").unlink()
    data = bytearray((tmp_path / "c1.jpg").read_bytes()); data[-3] ^= 0xFF      # same size, different bytes
    (tmp_path / "c1.jpg").write_bytes(bytes(data))
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 0 and res["held"] == 2 and ph.deleted == []


def test_the_phone_keeps_a_file_that_changed_since_the_copy(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a])
    p = con.execute("SELECT phone_path FROM files WHERE id=?", (a,)).fetchone()[0]
    ph.files[p] = "different"
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 0 and res["failed"] == 1 and p in ph.files


def test_a_copy_stays_when_the_copy_it_was_kept_for_is_deleted_too(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    con.execute("UPDATE suggestions SET bucket='copies', rule='C1', keep_id=? WHERE file_id=?", (a, b))
    con.commit()
    review.save(con, [a, b])
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 1 and res["held"] == 1
    assert ph.deleted == [con.execute("SELECT phone_path FROM files WHERE id=?", (a,)).fetchone()[0]]


def test_a_copy_stays_when_the_kept_one_is_not_on_the_phone(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    con.execute("UPDATE suggestions SET bucket='copies', rule='C1', keep_id=? WHERE file_id=?", (a, b))
    con.execute("DELETE FROM suggestions WHERE file_id=?", (a,))
    con.commit()
    review.save(con, [b])
    del ph.files[con.execute("SELECT phone_path FROM files WHERE id=?", (a,)).fetchone()[0]]
    res = apply(con, ph, log=lambda *a: None)
    assert res["deleted"] == 0 and res["held"] == 1


def test_an_imported_folder_can_never_delete(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    con.execute("DELETE FROM meta WHERE key='serial'"); con.commit()
    review.save(con, [a])
    with pytest.raises(WrongPhone):
        apply(con, ph, log=lambda *a: None)


def test_a_phone_without_sha1sum_deletes_nothing(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    review.save(con, [a])
    ph.can_hash = lambda: False
    with pytest.raises(WrongPhone, match="sha1sum"):
        apply(con, ph, log=lambda *a: None)
    assert ph.deleted == []


def test_labelled_junk_and_ticked_are_deleted_when_asked(con, tmp_path):
    from sweep.apply import approve_ticked
    (a, b), ph = setup(con, tmp_path)
    c = add(con, card(tmp_path / "c.jpg", seed=12), "wa_sent")
    pull.index(con)
    ph.files[con.execute("SELECT phone_path FROM files WHERE id=?", (c,)).fetchone()[0]] = \
        con.execute("SELECT sha1 FROM files WHERE id=?", (c,)).fetchone()[0]
    con.execute("INSERT INTO labels(file_id, label, at) VALUES(?, 'junk', 0)", (c,)); con.commit()
    review.save(con, [], keep_ids=[])            # the person saw nothing on the page
    assert approve_ticked(con) == 2               # a and b were ticked
    res = apply(con, ph, log=lambda *x: None, labels=True)
    assert res["deleted"] == 3


def test_labels_are_not_deleted_unless_asked(con, tmp_path):
    (a, b), ph = setup(con, tmp_path)
    con.execute("INSERT INTO labels(file_id, label, at) VALUES(?, 'junk', 0)", (a,)); con.commit()
    review.save(con, [], keep_ids=[])
    assert apply(con, ph, log=lambda *x: None)["deleted"] == 0
