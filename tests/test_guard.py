import datetime as dt
from sweep import guard


def row(i, folder="wa_images", sha=None, dh=None, mtime=1_700_000_000, w=500, h=500):
    return {"id": i, "folder": folder, "sha1": sha or f"s{i}", "dhash": dh or f"{i * 0x1f1f1f1f1f1f1f1f % (1 << 64):016x}",
            "phone_path": f"/p/IMG-{i}.jpg", "mtime": mtime, "width": w, "height": h, "bytes": 1000}


def v(kind, words="", people=False, screen=""):
    return {"kind": kind, "words": words, "people": people, "screen": screen}


def test_people_photos_and_documents_are_never_suggested():
    rows = [row(1), row(2)]
    out = guard.decide(rows, {1: v("photo", "", True), 2: v("document", "Apollo Pharmacy Bill")}, {})
    assert out == {}


def test_no_answer_means_keep():
    out = guard.decide([row(1)], {1: {"kind": None, "words": "", "people": None, "screen": ""}}, {})
    assert out == {}


def test_forward_set_aside_only_when_its_words_were_read():
    rows = [row(1), row(2), row(3)]
    vs = {1: v("greeting", "Good Morning, have a blessed Sunday"), 2: v("greeting", ""), 3: v("greeting", "शुभ प्रभात")}
    out = guard.decide(rows, vs, {})
    assert out[1][0] == "forwards" and "It reads" in out[1][1]
    assert 2 not in out                       # it could not read it, and it came once: kept
    assert out[3][0] == "forwards"


def test_unreadable_forward_that_arrived_twice_goes():
    rows = [row(1, dh="0000ffff0000ffff"), row(2, dh="0000ffff0000fffe")]   # look the same
    out = guard.decide(rows, {1: v("greeting"), 2: v("greeting")}, {})
    assert out[1][0] == "forwards" and "arrived 2 times" in out[1][1]


def test_greeting_with_real_people_is_your_call_not_ticked():
    out = guard.decide([row(1)], {1: v("greeting", "Happy Diwali from the Sharmas", people=True)}, {})
    assert out[1][0] == "check" and not guard.PRESELECT["check"]


def test_a_greeting_in_the_camera_folder_is_kept():
    out = guard.decide([row(1, folder="camera")], {1: v("greeting", "Good Morning")}, {})
    assert out == {}


def test_exact_copies_keep_one():
    rows = [row(1, folder="wa_sent", sha="x"), row(2, folder="camera", sha="x")]
    out = guard.decide(rows, {1: v("photo", people=True), 2: v("photo", people=True)}, {})
    assert list(out) == [1] and out[1][0] == "copies" and out[1][3] == 2


def test_old_otp_screenshot_suggested_unticked_recent_kept():
    now = dt.datetime(2026, 10, 25).timestamp()
    rows = [row(1, folder="screenshots", mtime=now - 60 * 86400), row(2, folder="screenshots", mtime=now - 2 * 86400)]
    vs = {1: v("screenshot", "Your OTP is 4821", screen="otp"), 2: v("screenshot", "Your OTP is 1111", screen="otp")}
    out = guard.decide(rows, vs, {}, today=now)
    assert out[1][0] == "screenshots" and not guard.PRESELECT["screenshots"] and 2 not in out


def test_blurry_needs_code_and_model_to_agree():
    rows = [row(1, folder="camera"), row(2, folder="camera")]
    chk = {1: {"blur": 3.0, "dark": 10.0}, 2: {"blur": 3.0, "dark": 10.0}}
    out = guard.decide(rows, {1: v("accidental"), 2: v("photo", people=True)}, chk)
    assert out[1][0] == "blurry" and 2 not in out


def test_model_alone_baseline_throws_away_any_forward_label():
    assert guard.model_alone(v("greeting", "Happy Diwali", people=True))
    assert not guard.model_alone(v("photo"))


def test_arrived_again_counts_only_lookalikes_that_are_forwards_too():
    rows = [row(1, dh="00000000ffffffff"), row(2, dh="00000000fffffffe")]   # look the same
    out = guard.decide(rows, {1: v("greeting"), 2: v("photo", people=True)}, {})
    assert 1 not in out and 2 not in out


def test_screenshot_type_falls_back_to_its_words():
    now = dt.datetime(2026, 10, 25).timestamp()
    rows = [row(1, folder="screenshots", mtime=now - 50 * 86400)]
    out = guard.decide(rows, {1: v("news", "Amazon Your package was delivered")}, {}, today=now)
    assert out[1][0] == "screenshots" and "Delivery tracking" in out[1][1]


# --- review findings, 24 Oct (B2, B3, M5) ---

def test_any_forward_kind_with_people_is_your_call():
    """A newspaper clipping about a grandchild ("news" + real people) must not be ticked."""
    out = guard.decide([row(1)], {1: v("news", "Dainik Bhaskar: Rohan tops district board exam", people=True)}, {})
    assert out[1][0] == "check"


def test_unknown_people_counts_as_maybe_people():
    out = guard.decide([row(1)], {1: v("greeting", "Shubh Deepawali Sharma parivaar", people=None)}, {})
    assert out[1][0] == "check"


def test_a_dark_video_is_never_called_blurry():
    rows = [dict(row(1, folder="camera"), phone_path="/p/VID_20261024.mp4")]
    out = guard.decide(rows, {1: v("accidental")}, {1: {"blur": 3.0, "dark": 12.0}})
    assert out == {}


def test_blurry_needs_the_model_to_say_no_people():
    rows = [row(1, folder="camera")]
    out = guard.decide(rows, {1: v("accidental", people=None)}, {1: {"blur": 3.0, "dark": 12.0}})
    assert out == {}


def test_copies_of_a_forward_all_go_as_forwards():
    """When the kept copy is itself a forward, its copies are forwards too, not 'copies of a kept file'."""
    rows = [row(1, sha="x"), row(2, folder="wa_sent", sha="x")]
    vs = {1: v("greeting", "Good Morning"), 2: v("greeting", "Good Morning")}
    out = guard.decide(rows, vs, {})
    assert out[1][0] == "forwards" and out[2][0] == "forwards"


def test_copies_of_a_family_photo_keep_exactly_one():
    rows = [row(1, sha="x"), row(2, folder="wa_sent", sha="x"), row(3, folder="wa_sent", sha="x")]
    vs = {i: v("photo", people=True) for i in (1, 2, 3)}
    out = guard.decide(rows, vs, {})
    assert 1 not in out and out[2][0] == "copies" and out[3][0] == "copies" and out[2][3] == 1


def test_build_ignores_files_already_deleted_or_gone(con, tmp_path):
    from conftest import add, card
    from sweep import pull
    a = add(con, card(tmp_path / "a.jpg"), "wa_images")
    pull.index(con)
    con.execute("INSERT INTO verdicts(file_id, kind, words, people, screen) VALUES(?,?,?,?,?)",
                (a, "greeting", "Good Morning", 0, ""))
    con.execute("INSERT INTO deletions(file_id, ok, message, at) VALUES(?,1,'deleted',0)", (a,))
    con.commit()
    assert guard.build(con) == {}
