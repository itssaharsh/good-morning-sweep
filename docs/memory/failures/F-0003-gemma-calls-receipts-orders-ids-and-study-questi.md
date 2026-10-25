---
id: F-0003
type: failure
title: Gemma calls receipts, orders, IDs and study questions 'ad' or 'quote'
status: active
scope: project
components: sweep/guard.py, sweep/wordlist.py
triggers: ad, quote, receipt, sent, real phone
evidence: commit:ed81f46
verified_at: 2026-10-25@ed81f46
relates: 
supersedes: 
helpful: 0
harmful: 0
cite_hash: e3b0c44298fc1c14
created: 2026-10-25
source: unknown
---

First real phone (builder's, 26 Oct): F1 ticked 134 'forwards' among pictures the builder KEPT, incl. an electricity recharge receipt, a food order, an Aadhaar notice, a maths problem, a Bubble Sort quiz, a 'Dear X, Congratulations' letter and personal birthday posts. The synthetic dev set had none of these. Fix: forward word required for every forward kind; DOCUMENT_WORDS (bill, receipt, order, ID, ticket, bank, card, letter, exam) always keep; Sent folders never ticked (P2 -> Your call). Ticked went 134 -> 10, all verified by eye. Rules changed after looking at the kept set: its numbers are labelled 'after fixes found on this set'; the bin set stayed unseen.