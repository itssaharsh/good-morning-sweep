---
id: ADR-0001
type: decision
title: Gemma answers in JSON, not a one-line format
status: active
scope: project
components: sweep/model.py
triggers: speed, prompt format, accuracy
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

Tested 24 Oct on the 38-picture dev set: a 'KIND | PEOPLE | SCREEN | WORDS' line cut generation from ~22 to ~12 tokens (5.6 s vs 6.7 s per picture) but broke PEOPLE (wrote 'people' for landscapes), called an OTP screenshot 'accidental' and a chat screenshot 'greeting'. JSON kept. Server: -t 12, 70-token image budget, thinking off via chat_template_kwargs.