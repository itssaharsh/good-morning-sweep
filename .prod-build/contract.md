# Implementation contract - good-morning-sweep
mode: hackathon | channel: direct | created: 2026-10-24
Upstream read: week3/entry/cards.json (prod-idea write-up cards, gate 0 FAIL / 0 WARN), the round's template and rules
(read 24 Oct 2026), the builder's scope note (24 Oct 21:20 UTC: "generic suggester for all photos").

## 1. Intent
- User and moment of use: a parent whose Android phone says "Storage almost full", mostly from WhatsApp forwards,
  and the son or daughter who gets asked to fix it. The phone is plugged into the family laptop once; the laptop
  works through the pictures slowly (hours), then the parent reviews one page of suggestions.
- What they can do: see every picture the tool suggests removing, grouped by reason, with the reason written on
  each (the words it read, the copy it kept, the blur it measured); untick anything to keep; delete the rest from
  the phone in one step, with a backup left on the laptop.
- Before -> after: before, hours of scrolling and deleting by hand, or a paid cloud plan, or a new phone; after, a
  10-minute review and gigabytes back, with no family photo or document touched.

## 2. Scope
MUST:
- M1 `sweep pull`: copy the phone's WhatsApp Images, WhatsApp Video, Screenshots and Camera folders to the laptop
  over adb (the backup), with an inventory (path, size, hash, date).
- M2 `sweep scan`: Gemma 4 E2B (local, llama.cpp) looks at each WhatsApp image, WhatsApp video keyframe and
  screenshot and returns a kind (greeting, quote, meme, news, ad, personal photo, document, screenshot type, other)
  and the words printed on it. Resumable: every answer is stored as it arrives; a restart picks up where it stopped.
- M3 code checks: exact and near duplicates (hash and perceptual hash, keep the sharpest/largest copy), blur and
  darkness scores.
- M4 the guard (decides suggestions; deterministic): people photos, documents and anything unsure are never
  suggested; a forward is suggested only when the model quoted its words and they match the forward word list, or
  the same picture arrived more than once; screenshots go to a separate "probably not needed" group.
- M5 `sweep review`: a local page, one screen per group, big thumbnails, the reason on each, tap to keep, a total of
  space to be freed, and "Delete from phone".
- M6 `sweep apply`: delete the approved files from the phone over adb, rescan the phone's gallery, record the space
  freed. Files stay on the laptop.
- M7 `sweep report`: counts, space freed, time taken, model-alone versus guarded numbers on a labelled sample.
SHOULD:
- S1 durable overnight run (Temporal workflow) that survives a crash or a closed laptop lid and retries a model call
  that timed out; only if M1-M7 are done by Sunday 03:00 UTC.
- S2 a static demo of the review page on public sample pictures, for a judge who clicks.
COULD: iPhone route; Hindi labels in the review page; a scheduled nightly run.
WON'T now: an Android app; cloud processing of family photos; face recognition.

## 3. Core journeys
J1: plug phone -> `sweep pull` -> folder copied, inventory printed (files, GB).
J2: `sweep scan` -> each picture gets kind + words + scores, progress shown, resumable -> suggestions computed.
J3: `sweep review` -> parent sees groups with reasons, unticks keepers -> approved list saved.
J4: `sweep apply` -> approved files deleted from phone, gallery refreshed -> "freed X GB" and storage before/after.

## 4. Surfaces
CLI (`sweep`), one local review page (served by the CLI), a static demo copy of that page.

## 5. States per journey
| Journey | initial | loading | success | error | empty | partial | retry | disabled | permission-denied | offline/degraded |
|---|---|---|---|---|---|---|---|---|---|---|
| J1 pull | no device: steps to connect | per-folder progress | inventory | adb error shown | folder missing: skipped, said | some folders copied | re-run copies only missing files | n/a | phone asks "Allow USB debugging?": message says tap Allow | n/a (local) |
| J2 scan | nothing scanned | n/N with ETA | all classified | model call failed: retried 3x then marked unsure (kept) | no pictures: says so | stopped midway: resumes | automatic | n/a | n/a | model server down: waits and retries, never guesses |
| J3 review | suggestions page | thumbnails lazy-load | approved list saved | save failed shown | nothing to suggest: says so | partial review saved | reload keeps choices | delete button off until review saved | n/a | n/a (local) |
| J4 apply | approved list | per-file progress | freed X GB | file missing on phone: skipped, logged | nothing approved: no-op | some deleted: report says which | re-run skips done | refuses without a review | adb unauthorised: message | n/a |

## 6. Data
| Entity | Key fields | Relationships | Owner | Persistence | Source of truth | PII/retention |
|---|---|---|---|---|---|---|
| file | phone path, local path, bytes, sha1, dhash, folder, mtime | 1 verdict, 0..1 dup group | parent | SQLite `sweep.db` beside the copy | the phone | family photos: stay on the laptop only; never uploaded |
| verdict | kind, words, model ms, blur, dark, raw model JSON | file | tool | SQLite | model + code | same |
| suggestion | reason, group, bytes, decided_by (guard rule) | file | tool | SQLite | guard | same |
| decision | keep/delete, who, when | suggestion | parent | SQLite | review page | same |

## 7. Interfaces
| Interface | Purpose | Contract file | Failure mode | Fallback |
|---|---|---|---|---|
| adb (Windows adb.exe from WSL) | list, pull, delete, media rescan | sweep/phone.py | not authorised, cable drop | clear message; resumable copy |
| llama-server (Gemma 4 E2B Q4_0 + mmproj, OpenAI-style API, localhost) | kind + words per image | sweep/model.py | timeout, crash, bad JSON | retry 3x; parse leniently; else "unsure" (kept) |
| ffmpeg | one keyframe per video | sweep/media.py | unreadable video | marked unsure (kept) |
| browser (review page) | parent's decisions | sweep/review.py | none beyond local | choices saved per tap |

## 8. Constraints
- Privacy: family photos never leave the laptop; no cloud model; the review page binds to 127.0.0.1.
- Safety: nothing is deleted from the phone without a saved review; every deleted file still exists on the laptop.
- Platform: Windows 11 laptop, WSL2, i7-13620H CPU, no GPU, ~7.6 GB RAM for WSL. Model speed measured 24 Oct:
  ~8.6 s per image at a 70-token image budget, cold; less with the prompt cached.
- Budget: ₹0 in API fees; AWS allowed up to $150 for development speed only (not for family photos).
- Deadline: freeze 2026-10-25T17:00Z, close 2026-10-26T06:59Z.
- Judging criteria (verbatim, rules post read 24 Oct 2026): Writing Quality (weighted most heavily) "clear and
  engaging; what you built, who it's for, why open innovation matters"; Relevance "Is open-source AI or open-weight
  models at the core of the project? Does it genuinely save effort, help someone relax, or make life easier?";
  Creativity "an original idea, or a fresh take on a familiar problem?"; Technical Execution "Does it work, and is it
  well built?"; partner tech optional. "If you can show us how much time or effort your project saves, even better!"

## 9. Acceptance criteria
AC-1: WHEN a phone is connected and authorised THE SYSTEM SHALL copy the four folders and print files and GB. Verify: run on the builder's phone. must
AC-2: WHEN scan runs on an image THE SYSTEM SHALL store kind, words, scores and the raw answer, and a restart SHALL skip stored ones. Verify: tests/test_scan.py (fake model) + kill/restart run. must
AC-3: WHEN the model says "personal photo" or "document", or returns nothing usable, THE SYSTEM SHALL never suggest the file. Verify: tests/test_guard.py. must
AC-4: WHEN the model calls a picture a forward but quotes no words matching the forward list and it has no duplicate THE SYSTEM SHALL keep it (unsure). Verify: tests/test_guard.py. must
AC-5: WHEN two files are exact or near duplicates THE SYSTEM SHALL suggest all but the sharpest/largest. Verify: tests/test_dupes.py. must
AC-6: WHEN the review is saved THE SYSTEM SHALL delete only approved files from the phone and report bytes freed. Verify: tests/test_apply.py (fake adb) + builder's phone dry run. must
AC-7: WHEN no review is saved THE SYSTEM SHALL refuse to delete. Verify: tests/test_apply.py. must
AC-8: WHEN the model server is down THE SYSTEM SHALL wait and retry, never guess. Verify: tests/test_scan.py. must

## 10. Assumptions
A-1 [safe]: the parent's phone is Android with WhatsApp media under Android/media/com.whatsapp/WhatsApp/Media - validate on the builder's phone.
A-2 [reversible]: a 70-token image budget is enough to read greeting text - validate on the dev set (words quoted verbatim on the first test).
A-3 [reversible]: adb delete + media rescan frees the space and updates the gallery - validate on the builder's phone with one test file.
A-4 [blocking, answered]: the parent agrees to the run and to being quoted as "my mom"/"my dad" - builder confirmed 24 Oct.

## 11. Demo script
1. "Storage almost full" on the parent's phone (screenshot). 2. Plug in, `sweep pull`: 9,000 files, 14 GB (real numbers
from the run). 3. `sweep scan` progress overnight; one card with the words Gemma read. 4. Review page: groups, a
false alarm the guard stopped (a family photo with "Happy Diwali" written on it, kept). 5. Delete; storage after.
6. The parent's reaction, verbatim.

## 12. Event and judged path
- Window: start 2026-10-19T20:07Z (20 Oct 01:37 IST), freeze 2026-10-25T17:00Z (25 Oct 22:30 IST, our choice for
  a write-up round), end 2026-10-26T06:59Z (26 Oct 12:29 IST); source https://dev.to/challenges/hacktoberfest-week3-2026-10-19.
  Rule on earlier work (FAQ, read 24 Oct 2026): "all submissions and their respective repositories must be started
  and completed within the challenge window. Commits made after the submission deadline must be noted in the
  project's readme."
- How an entry is judged: the DEV post is the entry ("This is DEV, so your write-up matters most"); a judge reads the
  post, may open the demo link or video and the repository; no statement that anyone runs the build.
- Criteria: as in §8, read from the rules post on 24 Oct 2026; Writing Quality weighted most heavily, others unweighted.
- Criteria coverage: Writing -> the parent's story, the failure told in her terms, her words (open until Sunday's run);
  Relevance -> Gemma 4 does the paid job (reading each picture), time saved measured (open); Creativity -> the guard
  "reads before it deletes" and the forwards angle no other entry took (planned); Technical -> resumable scan,
  model-alone vs guarded table on a held-out phone (open).
- Capability coverage: Gemma (featured lane) -> Gemma 4 E2B classifies every WhatsApp picture locally (M2). Temporal
  (partner lane) -> only if S1 ships: the overnight scan as a workflow that resumes after a crash. No other lane.
- Main result: a phone's storage before and after (screenshots), the parent's own pictures, on material we did not
  author; the staged failure it holds through: a family photo with festival text on it, kept by the guard.
- Promise coverage: "Good Morning" -> the forwards bucket and its count on the first screen of the review page;
  "Sweep" -> the "Delete from phone" step and GB freed.
- Before the clock: nothing was built before the start; the idea was chosen and tools were installed on 24 Oct, after
  the window opened. The README says the repository was started on 24 Oct 2026 inside the window.

### Model core (write-up track)
- Model: Gemma 4 E2B (open weights), Q4_0 GGUF, llama.cpp on the laptop CPU.
- Paid job: looking at each WhatsApp picture and screenshot and saying what it is and what it says (M2, a MUST).
- Without the open model, the product can only find exact duplicates and blurry shots; it can no longer tell a
  good-morning card from a photo of the grandchild, so it cannot suggest any forward safely.
- Guard: M4.
