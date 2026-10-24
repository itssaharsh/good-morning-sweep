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


def test_parse_is_lenient():
    assert model.parse('```json\n{"kind": "Greeting", "words": "Shubh Prabhat", "people": "false"}\n```')["kind"] == "greeting"
    assert model.parse("kind: meme")["kind"] == "meme"
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


def test_lookalikes_are_asked_once(con, tmp_path):
    from PIL import Image
    a = card(tmp_path / "a.jpg", seed=4)
    Image.open(a).save(tmp_path / "b.jpg", quality=55)      # same card, compressed again
    add(con, a, "wa_images"); add(con, tmp_path / "b.jpg", "wa_images")
    pull.index(con)
    f = fake(['{"kind":"greeting","words":"Good Morning","people":false,"screen":""}'])
    scan.run(con, classify=f, log=lambda *a: None)
    assert len(f.calls) == 1
    assert con.execute("SELECT COUNT(*) FROM verdicts WHERE kind='greeting'").fetchone()[0] == 2
