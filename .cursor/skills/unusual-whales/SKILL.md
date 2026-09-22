---
name: unusual-whales-api
description: Query the Unusual Whales API for unusual options flow, dark pool prints, market tide, stock greek exposure, financial statements, shorts/borrow, and technical indicators. Use when calling UW, GEX, flow, or interpreting UW JSON.
---

# Unusual Whales (Cursor)

Canonical copy for this repo and for cloud/other LLMs:

- API skill: `prompts/unusual-whales/SKILL.md`
- Daily-report analysis prompt: `prompts/unusual-whales/daily-report.md`

**Read `prompts/unusual-whales/SKILL.md` before any Unusual Whales API call.** Do not invent endpoints. Auth is `Authorization: Bearer` plus `UW-CLIENT-API-ID: 100001`. Token from env, never from these files.

If those two disagree, `prompts/` wins.
