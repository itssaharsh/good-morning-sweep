---
id: F-0002
type: failure
title: A review page that shows 600 tiles can delete thousands unseen
status: active
scope: project
components: sweep/review.py, sweep/apply.py, sweep/phone.py
triggers: review page, delete, unseen, cmdline limit, sha1
evidence: commit:c346235
verified_at: 2026-10-24@c346235
relates: 
supersedes: 
helpful: 0
harmful: 0
cite_hash: e3b0c44298fc1c14
created: 2026-10-24
source: unknown
---

Fresh-eyes review 24 Oct found: page capped at 600 tiles but deleted all selected; people only guarded for greeting/quote; videos judged by one frame; apply trusted size only, not serial or phone sha1; 200-path rm line exceeded Windows' 32,767-char limit; durable scan looped on unreadable answers. Fixed in c346235: seen-only deletion, P1 for any forward kind, pushed phone script that sha1-checks before rm, serial and suggested_at checks, per-batch recording. Lesson: a review step only protects what it actually shows; check bytes, not sizes, before deleting.