# Good Morning Sweep

**Frees a parent's phone from WhatsApp "Good Morning" forwards, duplicate copies and accidental shots, using an open
model (Gemma 4) that runs on the family laptop. It reads every picture before it suggests deleting one, and nothing is
deleted until a person has looked at the pile.**

Built for the [Hacktoberfest Open-Source AI Challenge, Week 3: Slow and Steady](https://dev.to/challenges/hacktoberfest-week3-2026-10-19).
The repository was started on 24 October 2026, inside the challenge window.

## What it does

Plug the phone into a laptop over USB. `sweep run` copies the phone's WhatsApp Images, WhatsApp Video, Screenshots and
Camera folders to the laptop (that copy is the backup), then works through them slowly, one picture at a time, with
Gemma 4 E2B running locally through llama.cpp. When it is done, `sweep review` opens a page that groups what it would
remove and says why on every picture:

| Group | Who decides | Ticked when the page opens |
|---|---|---|
| WhatsApp forwards (good-morning cards, quotes, memes, forwarded news and ads, forwarded videos) | Gemma says what it is **and quotes the words printed on it**; the code checks those words | yes |
| Exact copies | code (same bytes); the original is kept | yes |
| Blurry or black shots | code measures blur and brightness, and Gemma must agree it is a mistake | yes |
| Almost the same photo | code (pictures that look the same); the sharper one is kept | no |
| Old screenshots of one-time codes, payments, deliveries | Gemma reads them; older than 30 days | no |
| Your call | a "greeting" with real people in it, such as a family photo with "Happy Diwali" written on it | no |

Photos of people, documents (bills, prescriptions, IDs, tickets) and anything the model is unsure about are never
suggested. Tap a picture to keep it, then press **Delete from phone**. Files are deleted on the phone only; the laptop
keeps its copy of every one.

## Why it is slow, and why that is fine

On a laptop CPU with no graphics card, Gemma 4 E2B takes 6 to 8 seconds per picture. A parent's phone with a few
thousand WhatsApp pictures is an overnight job. The scan stores each answer the moment it arrives and goes through the
biggest files first, so it can be stopped at any time and the first hours already find most of the space. Identical
files and look-alike forwards are asked about once. `sweep scan --durable` runs the same scan as a
[Temporal](https://temporal.io) workflow: if the model server crashes, each call is retried with back-off until it
answers (it never guesses), and if the laptop sleeps, running the command again carries on from the next picture.

## Why an open model

The pictures are a family's photos. They stay on the laptop: no upload, no account, no API bill for thousands of
images. The model is a 3 GB file that runs on the CPU of an ordinary laptop.

## Run it

Needs Python 3.10+, [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`), Android platform-tools
(`adb`), ffmpeg for video frames, and the Gemma 4 E2B GGUF with its vision projector
(`gemma-4-E2B-it-Q4_0.gguf` and `mmproj-gemma-4-E2B-it-Q8_0.gguf`).

```bash
pip install -e .
scripts/start-model.sh            # Gemma 4 E2B on 127.0.0.1:8089 (set MODEL, MMPROJ, LLAMA_DIR if needed)
# on the phone: Settings > About phone > tap Build number 7 times; Developer options > USB debugging on; plug in; Allow
sweep devices                     # the phone, its Android version and free space
sweep run                         # copy, index, let Gemma look, compute suggestions (hours; stop and re-run any time)
sweep review                      # http://127.0.0.1:8765 : tap to keep, then "Delete from phone"
sweep report                      # what was found and freed
```

Step by step instead: `sweep pull`, `sweep scan` (or `sweep scan --durable` with `temporal server start-dev` running),
`sweep suggest`, `sweep review`, `sweep apply`. `sweep import <folder>` works on a folder of pictures instead of a phone.
`sweep label` opens a page to label a random sample as junk or keep, and `sweep report` then scores the model alone
against the guard on it.

## How it is built

```
phone --adb pull--> laptop copy --index--> sha1, look-alike hash, blur, brightness   (code)
                                  \--scan--> Gemma 4 E2B: kind, words on it, real people?, screenshot type   (model)
                                              \--guard--> suggestions with a written reason   (plain rules)
                                                           \--review page--> person keeps or deletes --adb rm--> phone
```

- `sweep/model.py`: the prompt and the llama.cpp client. Fixed instructions first, picture last, so the server reuses
  the cached instructions; a 70-token picture budget; Gemma's thinking switched off.
- `sweep/guard.py`: every rule that decides a suggestion, each with its id (C1, F1, F2, P1, B1, S1, N1).
- `sweep/scan.py` and `sweep/durable.py`: the resumable scan, plain and as a Temporal workflow.
- `sweep/phone.py`, `sweep/pull.py`, `sweep/apply.py`: adb, the copy, and deletion that refuses without a saved review
  or without a complete laptop copy of the file.
- `tests/`: the rules above as tests (`python -m pytest`).

## Limits

- Android only (adb). iPhones are not supported.
- On Android 10 and older, the gallery may show deleted pictures until it rescans.
- Deleting a WhatsApp picture from the phone leaves "media missing" in that chat, as deleting it from the gallery does.
- The model reads Hindi and English best; other scripts are covered by the word list but tested less.

## License

MIT
