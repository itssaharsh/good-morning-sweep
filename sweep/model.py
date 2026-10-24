"""Gemma 4 on the laptop, through llama.cpp's server. One question per picture: what is it, and what does it say?"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

MODEL_URL = os.environ.get("SWEEP_MODEL_URL", "http://127.0.0.1:8089")
MODEL_NAME = "gemma-4-E2B-it Q4_0 (llama.cpp, CPU)"

KINDS = ("greeting", "quote", "meme", "news", "ad", "photo", "document", "screenshot", "accidental", "other")
SCREENS = ("otp", "payment", "delivery", "chat", "social", "map", "other")

# Fixed text first and the picture last, so llama.cpp reuses the cached instructions for every picture.
SYSTEM = """You sort pictures from a parent's phone so they can free up space. For the picture you are shown, answer with one line of JSON and nothing else:
{"kind": K, "words": W, "people": P, "screen": S}
K is one of:
- "greeting": a good morning, good night, festival, blessing or god picture made to be forwarded
- "quote": a motivational, religious or life quote made to be forwarded
- "meme": a joke or funny picture made to be forwarded
- "news": forwarded news, health tips, warnings, political or viral text graphics
- "ad": an advertisement, offer, flyer or promotion
- "photo": a real photograph someone took (people, places, food, pets, events)
- "document": an ID card, bill, receipt, prescription, ticket, form, letter, certificate or handwritten note
- "screenshot": a screenshot of a phone or computer screen (status bar, app bars, chat bubbles, notifications); use this whatever the screen shows
- "accidental": a blurry, black, pocket or floor shot taken by mistake
- "other": none of these
W: the main words printed on the picture, copied exactly as written, in their own language and script (at most 15 words), or "" if there are none.
P: true if it shows real people in a real photograph (not drawings, not gods, not models in a greeting card), else false.
S: only for screenshots, one of "otp", "payment", "delivery", "chat", "social", "map", "other"; otherwise "".
"""
ASK = "Answer with the JSON line."


class ModelDown(RuntimeError):
    pass


def healthy(url=MODEL_URL, timeout=5):
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_until_up(url=MODEL_URL, wait_s=600, every=10, log=print):
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if healthy(url):
            return True
        log(f"model server not answering at {url}; waiting (never guessing)...")
        time.sleep(every)
    return False


REQUIRED = ("kind", "words", "people", "screen")


def parse(text):
    """The model's answer as a dict. Lenient about code fences, case and "false" written as a string, strict about
    the rest: an answer that is not complete JSON with all four fields is unreadable (kind None), so a cut-off answer
    can never keep its kind while losing whether real people are in the picture."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict) or any(k not in data for k in REQUIRED):
        return {"kind": None, "words": "", "people": None, "screen": ""}
    kind = str(data.get("kind", "")).strip().lower()
    screen = str(data.get("screen", "") or "").strip().lower()
    people = data.get("people")
    if isinstance(people, str):
        people = {"true": True, "false": False}.get(people.strip().lower())
    return {
        "kind": kind if kind in KINDS else None,
        "words": str(data.get("words", "") or "").strip()[:300],
        "people": people if isinstance(people, bool) else None,
        "screen": screen if screen in SCREENS else "",
    }


def ask(jpeg, url=MODEL_URL, timeout=300, max_tokens=160):
    """One request. Returns (raw text, ms, finish reason). Raises ModelDown when the server can't be reached."""
    b64 = base64.b64encode(jpeg).decode()
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": ASK},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},  # Gemma 4 thinks aloud after a system message otherwise
    }
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"model server error {e.code}: {e.read()[:200]!r}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise ModelDown(str(e))
    ms = int((time.time() - t0) * 1000)
    choice = data["choices"][0]
    return choice["message"].get("content") or "", ms, choice.get("finish_reason") or ""


def classify(jpeg, url=MODEL_URL, attempts=3, asker=ask, log=print, wait_s=600):
    """kind, words, people, screen, raw, ms; or raises ModelDown after waiting. A picture that keeps failing
    returns kind None with the error, and the guard keeps it."""
    last = None
    for i in range(attempts):
        try:
            got = asker(jpeg, url)
            raw, ms = got[0], got[1]
            finish = got[2] if len(got) > 2 else ""
            out = parse(raw)
            if finish == "length":   # the answer was cut off: whatever it said is not trusted
                out = dict(out, kind=None)
                why = f"answer cut off at the token limit: {raw[:120]!r}"
            else:
                why = f"unreadable answer: {raw[:120]!r}"
            if out["kind"] is None and i + 1 < attempts:
                last = why
                continue
            return dict(out, raw=raw, ms=ms, error=None if out["kind"] else why)
        except ModelDown:
            if not wait_until_up(url, wait_s=wait_s, log=log):
                raise
        except Exception as e:  # a bad picture or a server error: try again, then give up on this one picture
            last = str(e)
            time.sleep(2 * (i + 1))
    return {"kind": None, "words": "", "people": None, "screen": "", "raw": "", "ms": 0, "error": last}
