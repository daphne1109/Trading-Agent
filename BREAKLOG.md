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

## 2026-10-07 MYT: Deriv's trading API isn't available to Malaysian residents
**What broke:** Registering the app on developers.deriv.com showed "These services are currently unavailable in your country of residence". No app ID and no account token, so the demo-account executor can't run.
**How it was found:** The P0 setup, before any live trade.
**Root cause:** Regulatory: Deriv doesn't serve Malaysian residents.
**Fix:** We did not work around it with a VPN or false residence. A read-only probe showed that the unauthenticated public endpoint (`/trading/v1/options/ws/public`) still serves live ticks and real Rise/Fall quotes. The agent now defaults to **paper mode**:
- It streams the live feed and gets a real quote for every trade.
- It runs every safety check unchanged.
- It fills on paper at the quoted price and settles on live ticks using Deriv's Rise/Fall rules.
- It refuses to settle if a reconnect or a tick gap makes the exit tick uncertain.

The demo-account executor stays, fully tested, behind `EXECUTION_MODE=demo`. The `risk-reviewer` approved the change after one round of fixes:
- repeated ticks after a reconnect could shift the exit tick;
- the rule-based stand-in model could have traded with no human approval in demo mode.

**Lesson:** Check legal availability as early as the technical API. Keep execution behind an interface, so a constraint changes one component, not the system.

## 2026-10-06 19:30 MYT: Executor could under-count open positions after an uncertain buy (caught before commit)
**What broke:** The first executor design marked a trade `error` whenever the `buy` call raised. That included a timeout or dropped connection *after* the request was sent, when the contract might be live. The trade was then left out of the open-position and P&L counts, so a retry could exceed the position limit and the daily loss cap. Review also found three more problems:
- "human approved" was just a boolean the caller passed in;
- an approval row with no expiry was accepted;
- an old decision could still execute.

**How it was found:** The `risk-reviewer` subagent. It took three review rounds before it approved.
**Root cause:** The design treated "the call failed" as "nothing happened". In a distributed system, a timeout means "unknown", not "no".
**Fix:**
- A new `unknown` trade status that fails closed: it counts as an open position for 10 minutes and as a full-stake loss for the day. Only a definite Deriv rejection counts as `error`.
- The executor reads the decision from the DB and checks approvals against the `approvals` table, including expiry and that the approval covers every current trigger.
- Decisions have a maximum age.
- The quote must equal the configured stake.
- Kill/pause is re-checked right before the buy.
- A single-instance Postgres advisory lock, whose loss halts trading.
- 26 executor tests cover each case.

**Lesson:** When a side effect may have happened, record "unknown" and let it count against your limits. Never record "failed".

## 2026-10-06 18:10 MYT: Risk gate approved trades it should have blocked (caught before commit)
**What broke:** The first RiskGate passed all 40 of its own tests, including a property test. An adversarial review still found inputs that produced APPROVE:
- a NaN or negative market-data age (`nan > 5` is False);
- negative position or loss counts;
- `confidence=True`;
- an empty rules list, which skipped even the kill switch;
- after the first fix, a look-alike function *named* `kill_switch` that always passed.

Some inputs (Decimal NaN, naive datetimes) raised exceptions instead of rejecting.
**How it was found:** The `risk-reviewer` subagent (`.claude/agents/risk-reviewer.md`) ran in the background and probed the gate with a scratch script of hostile values. A second review pass caught the name-spoofing gap.
**Root cause:** The rules compared values with `<` and `>` and assumed well-typed, finite inputs. NaN fails every comparison, so "not greater than the limit" read as "safe". The rule-set check matched rules by name instead of identity.
**Fix:**
- A `context_valid` rule rejects wrong types, non-finite values, negative counts and naive datetimes.
- Every rule also guards its own inputs, and the gate turns any exception into a failed rule.
- The required rules are checked by identity, and `RiskLimits` validates itself.
- The daily cap now counts stake already at risk.
- 70 new tests, including a never-raises property and the spoof case.
**Lesson:** Write safety checks as "allow only what is provably valid", never "block what looks bad". NaN and wrong types quietly pass any comparison written the other way.

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
