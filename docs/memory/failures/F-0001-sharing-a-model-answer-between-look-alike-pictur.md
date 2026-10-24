---
id: F-0001
type: failure
title: Sharing a model answer between look-alike pictures deletes family photos
status: active
scope: project
components: sweep/scan.py, sweep/checks.py, sweep/guard.py
triggers: dhash, near duplicate, look-alike, propagation
evidence: 
verified_at: 2026-10-24@b2582b8
relates: 
supersedes: 
helpful: 0
harmful: 0
cite_hash: e3b0c44298fc1c14
created: 2026-10-24
source: unknown
---

Attempt: ask Gemma once per look-alike group (dhash <= 5) and copy the answer. Failure: a photo inherited 'Happy Diwali from the Sharmas' from the same photo with text added and was suggested as a forward; flat pictures (black, white screenshots) all hash to ~0 so a black pocket shot inherited an Amazon screenshot's words. Fix: answers shared between identical bytes only; flat hashes (popcount <8 or >56) excluded from look-alike groups; F2 and N1 only pair same-kind pictures. Tests: test_lookalikes_are_asked_separately, test_flat_pictures_are_not_look_alikes, test_arrived_again_counts_only_lookalikes_that_are_forwards_too.