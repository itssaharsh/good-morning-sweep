import pytest
from sweep import model, pull, scan
from conftest import card, photo, add


def fake(answers):
    calls = []

    def classify(jpeg, url=None, log=None):
        calls.append(1)
        a = answers[len(calls) - 1] if len(calls) <= len(answers) else answers[-1]
        return dict(model.parse(a), raw=a, ms=5, error=None)
    classify.calls = calls
    return classify


def test_each_answer_is_stored_and_a_restart_skips_done(con, tmp_path):
    for i in range(3):
        add(con, card(tmp_path / f"c{i}.jpg", seed=i + 10), "wa_images")
    pull.index(con)
    f = fake(['{"kind":"greeting","words":"Good Morning","people":false,"screen":""}'])
    assert scan.run(con, limit=2, classify=f, log=lambda *a: None) == 2
    assert con.execute("SELECT COUNT(*) FROM verdicts WHERE kind='greeting'").fetchone()[0] == 2
    f2 = fake(['{"kind":"photo","words":"","people":true,"screen":""}'])
    assert scan.run(con, classify=f2, log=lambda *a: None) == 1          # only the one left
    assert len(f2.calls) == 1


def test_identical_files_are_asked_once(con, tmp_path):
    import shutil
    a = card(tmp_path / "a.jpg")
    shutil.copy(a, tmp_path / "b.jpg")
    add(con, a, "wa_images"); add(con, tmp_path / "b.jpg", "wa_sent")
    pull.index(con)
    f = fake(['{"kind":"greeting","words":"Good Morning","people":false,"screen":""}'])
    scan.run(con, classify=f, log=lambda *a: None)
    assert len(f.calls) == 1
    assert con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 2


def test_camera_photos_go_to_the_model_only_when_blurry_or_black(con, tmp_path):
    from PIL import Image
    Image.new("RGB", (300, 300), (2, 2, 2)).save(tmp_path / "black.jpg")
    add(con, tmp_path / "black.jpg", "camera")
    add(con, photo(tmp_path / "sharp.jpg"), "camera")
    pull.index(con)
    names = [r["local_path"].rsplit("/", 1)[-1] for r in scan.pending(con)]
    assert names == ["black.jpg"]


def test_parse_is_lenient_about_fences_case_and_string_booleans():
    p = model.parse('```json\n{"kind": "Greeting", "words": "Shubh Prabhat", "people": "false", "screen": ""}\n```')
    assert p["kind"] == "greeting" and p["people"] is False
    assert model.parse("kind: meme")["kind"] is None
    assert model.parse("no idea")["kind"] is None


def test_server_down_waits_and_never_guesses(monkeypatch):
    def down(jpeg, url):
        raise model.ModelDown("refused")
    monkeypatch.setattr(model, "wait_until_up", lambda *a, **k: False)
    with pytest.raises(model.ModelDown):
        model.classify(b"x", asker=down, log=lambda *a: None)


def test_unreadable_answers_become_unsure(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda s: None)
    out = model.classify(b"x", asker=lambda j, u: ("I think it's nice", 3), log=lambda *a: None)
    assert out["kind"] is None and out["error"]


def test_lookalikes_are_asked_separately(con, tmp_path):
    """A photo and the same photo with "Happy Diwali" written on it look alike; each gets its own answer."""
    from PIL import Image
    a = card(tmp_path / "a.jpg", seed=4)
    Image.open(a).save(tmp_path / "b.jpg", quality=55)
    add(con, a, "wa_images"); add(con, tmp_path / "b.jpg", "wa_images")
    pull.index(con)
    f = fake(['{"kind":"greeting","words":"Good Morning","people":false,"screen":""}'])
    scan.run(con, classify=f, log=lambda *a: None)
    assert len(f.calls) == 2


def test_a_cut_off_answer_is_unreadable():
    """Truncated JSON must not keep a forward kind while losing 'people' (review finding B2)."""
    p = model.parse('{"kind":"greeting","words":"शुभ दीपावली शर्मा परिवार')
    assert p["kind"] is None


def test_an_answer_missing_fields_is_unreadable():
    assert model.parse('{"kind":"greeting","words":"Good Morning"}')["kind"] is None


def test_answer_stopped_by_length_is_unreadable(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda s: None)
    full = '{"kind":"greeting","words":"Good Morning","people":false,"screen":""}'
    out = model.classify(b"x", asker=lambda j, u: (full, 3, "length"), log=lambda *a: None)
    assert out["kind"] is None and "cut off" in out["error"]


def test_a_file_with_an_unreadable_answer_is_not_offered_again(con, tmp_path):
    """Otherwise the durable workflow asks about it forever and never finishes (review finding M7)."""
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    pull.index(con)
    scan.store(con, con.execute("SELECT sha1 FROM files WHERE id=?", (a,)).fetchone()[0],
               {"kind": None, "words": "", "people": None, "screen": "", "raw": "?", "ms": 1, "error": "unreadable"})
    assert scan.pending(con) == []
    assert scan.retry_unsure(con) == 1 and len(scan.pending(con)) == 1


def test_camera_videos_are_not_sent_to_the_model(con, tmp_path):
    from PIL import Image
    p = tmp_path / "VID_1.mp4"
    Image.new("RGB", (64, 64), (2, 2, 2)).save(tmp_path / "x.jpg")
    p.write_bytes((tmp_path / "x.jpg").read_bytes())
    fid = add(con, p, "camera")
    con.execute("UPDATE files SET sha1='v', dhash='0000000000000000' WHERE id=?", (fid,))
    con.execute("INSERT INTO checks(file_id, blur, dark) VALUES(?, 1, 2)", (fid,))
    con.commit()
    assert scan.pending(con) == []


def test_a_time_limit_stops_the_scan_and_the_next_run_carries_on(con, tmp_path, monkeypatch):
    for i in range(3):
        add(con, card(tmp_path / f"t{i}.jpg", seed=40 + i), "wa_images")
    pull.index(con)
    clock = {"t": 1000.0}
    monkeypatch.setattr(scan.time, "time", lambda: clock["t"])
    def slow(jpeg, url=None, log=None):
        clock["t"] += 1800       # each picture takes half an hour
        return dict(model.parse('{"kind":"photo","words":"","people":true,"screen":""}'), raw="{}", ms=1, error=None)
    assert scan.run(con, classify=slow, log=lambda *a: None, hours=0.75) == 2
    assert len(scan.pending(con)) == 1
