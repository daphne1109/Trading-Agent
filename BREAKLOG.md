# BREAKLOG

Every incident is logged here when it happens: what broke, how it was found, the fix and the lesson. Newest first.

<!-- Template:
## YYYY-MM-DD HH:MM MYT: <short title>
**What broke:**
**How it was found:** (alert / test / dashboard / Telegram / by eye)
**Root cause:**
**Fix:** (commit link)
**Lesson:** (one general sentence)
-->

## 2026-10-06: Deriv API had moved to a new platform
**What broke:** The original design assumed the classic Deriv WebSocket API (`authorize` with a token, `VRTC` demo login IDs). That design would not have worked.
**How it was found:** We checked the current official docs before writing any client code.
**Root cause:** Deriv now serves trading through `api.derivws.com`: a REST call returns a one-time-password WebSocket URL, and demo and real accounts use separate endpoints (`/ws/demo`, `/ws/real`).
**Fix:** We redesigned the client around the OTP flow, and the demo guard now pins the `/ws/demo` endpoint instead of checking a login-ID prefix (PLAN.md §2, P0).
**Lesson:** Check third-party APIs against live docs before the first line of code; a plan written from memory is a guess.
