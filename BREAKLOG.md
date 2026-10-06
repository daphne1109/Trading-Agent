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

## 2026-10-06 17:30 MYT: Test suite hung forever on the first database test
**What broke:** `pytest` hung with no output as soon as an async Postgres fixture ran. Sync connections worked fine.
**How it was found:** The run timed out. A `faulthandler` dump showed the event loop idle inside the fixture, and a stand-alone script reproduced the hang on `AsyncConnection.connect()`.
**Root cause:** On Windows + Docker Desktop, `localhost` resolves to `::1` first. That IPv6 attempt was never answered, and with no `connect_timeout` the async driver waited forever instead of falling back to `127.0.0.1`.
**Fix:** Local URLs use `127.0.0.1`, and every connection and pool sets `connect_timeout`, so a bad host fails fast.
**Lesson:** Every network call in an always-on system needs an explicit timeout; "it will fail eventually" can mean never.

## 2026-10-06: Deriv API had moved to a new platform
**What broke:** The original design assumed the classic Deriv WebSocket API (`authorize` with a token, `VRTC` demo login IDs). That design would not have worked.
**How it was found:** We checked the current official docs before writing any client code.
**Root cause:** Deriv now serves trading through `api.derivws.com`: a REST call returns a one-time-password WebSocket URL, and demo and real accounts use separate endpoints (`/ws/demo`, `/ws/real`).
**Fix:** We redesigned the client around the OTP flow, and the demo guard now pins the `/ws/demo` endpoint instead of checking a login-ID prefix (PLAN.md §2, P0).
**Lesson:** Check third-party APIs against live docs before the first line of code; a plan written from memory is a guess.
