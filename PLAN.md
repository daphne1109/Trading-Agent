# Build Plan: Autonomous Trading Agent (Deriv demo account)

> **Status:** v1, written 2026-10-06 (Tue). This replaces the day table in `deriv-agent-HANDOFF.md` §6; the rest of the handoff still applies.
> **Deadline:** applications close about **2026-10-12 (Mon)**. The agent must be **live** by then and keep running afterwards.
> **Goal:** show *safe, observable, self-improving autonomous agent engineering*. Profit doesn't matter, and we never claim it.

---

## 0. How to read this plan

Each phase has the same parts:

- **Goal**: one sentence.
- **Deliverables**: files and features that exist at the end.
- **How**: the approach and key design decisions.
- **TODO**: checkboxes in the order to do them. 👤 marks steps **you** must do (accounts, keys, payments). Claude does everything else.
- **Unit tests**: named tests that must exist and pass.
- **Exit criteria**: what has to be true before the next phase starts. If one fails, the phase isn't done.
- **Cut if behind**: what to drop first if the phase runs long.

### Timeline at a glance

| Phase | Date | Theme | Exit criteria (short) |
|---|---|---|---|
| P0 | Tue 10-06 (AM) | Accounts, keys, API spike | Every external API answered once from a script |
| P1 | Tue 10-06 | Foundation + market feed | Ticks stream into Postgres for 1 h, survive a forced disconnect |
| P2 | Wed 10-07 | Decide → Risk → Execute + **deploy** | First autonomous demo trade placed **from the VM** |
| P3 | Thu 10-08 | Telegram human-in-the-loop + outcomes | Kill and resume from your phone; a trade waits for approval and settles |
| P4 | Fri 10-09 | Nightly reflection + web dashboard v1 | First automatic playbook revision; dashboard has a public link |
| P5 | Sat 10-10 | Replay eval + CI + AI review | CI blocks a deliberately worse decision prompt |
| P6 | Sun 10-11 | Polish: UI, README, video, hardening | A stranger can understand, run and watch it |
| P7 | Mon 10-12 | Metrics, CV, application note, submit | Application sent |

**Running from P2 onward:** once deployed on Wed, the agent never stops. Each later phase deploys with `git pull && docker compose up -d --build`.

---

## 1. Architecture

```
                        ┌────────────────────── VM (GCP e2-micro, Docker Compose) ──────────────────────┐
                        │                                                                               │
 Deriv REST ──OTP──▶ ┌──┴───────────┐  ticks  ┌────────────┐ snapshot ┌───────────────┐ typed answer ┌──────────┐
 api.derivws.com     │ DerivClient  │───────▶│ MarketFeed │─────────▶│ DecisionRouter│─────────────▶│ RiskGate │
 Deriv WS (demo) ◀──▶│ (reconnect,  │        │ (buffer,   │          │ Jev → Clef →  │              │ (pure    │
                     │  demo-guard) │        │ stale guard│          │ Claude Haiku  │              │  code)   │
                     └──┬───────────┘        │ indicators)│          └───────────────┘              └────┬─────┘
                        │ ▲                  └────────────┘                 ▲                  reject /  │  approve
                        │ │ buy / contract updates                          │ playbook.md      needs-approval
                        │ │                                                 │                       │    │
                        │ └──────────────── Executor ◀── approved ──── Telegram bot (you) ◀─────────┘    │
                        │                      │                                                         │
                        │                      ▼                                                         │
                        │              ┌───────────────┐   nightly   ┌──────────────────────┐            │
                        │              │   Postgres    │◀───────────▶│ Reflection (Haiku)   │            │
                        │              │ decision log  │             │ → playbook revision  │            │
                        │              └──────┬────────┘             └──────────────────────┘            │
                        │                     │ read-only role                                           │
                        │              ┌──────▼────────┐   Caddy (HTTPS)                                 │
                        │              │ Web dashboard │──────────────▶  public link for CV / interviews  │
                        │              └───────────────┘                                                 │
                        └────────────────────────────────────────────────────────────────────────────────┘
```

**Rules that never change** (copy into `CLAUDE.md`):
1. **Demo only.** The code connects only to `.../ws/demo`. Several independent checks refuse anything else (P2).
2. **The model proposes; code decides.** The model picks only a direction (`CALL` / `PUT` / `HOLD`) with probabilities. Stake, duration, limits and execution are deterministic code. No model output can skip `RiskGate`.
3. **Secrets live only in `.env`** (git-ignored). `.env.example` lists the variable names with no values.
4. **Every claim on the CV must be checkable** in the repo, the logs or the dashboard.
5. **`BREAKLOG.md` gets an entry for every incident** when it happens: what broke, how it was found, the fix, the lesson.
6. **Honesty about markets.** Deriv volatility indices come from a random number generator, so there's no real edge to find. The README says this. Evals measure *behaviour and safety*, not profit.

---

## 2. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | asyncio throughout |
| Deriv | `websockets` + `httpx` | New API: REST `https://api.derivws.com`, WS `wss://api.derivws.com/trading/v1/options/ws/demo?otp=…` |
| Decision model | TypeSafe **Jev** (`POST https://api.typesafe.ai/v1/systemone`), raw `httpx` | Fallbacks: Cloudflare **Clef-flash** (same API shape), then **Claude Haiku 4.5** |
| Writing model | Anthropic SDK, `claude-haiku-4-5` | Reflection + daily summary |
| Validation / config | Pydantic v2, `pydantic-settings` | All model outputs are parsed into Pydantic models |
| DB | PostgreSQL 16, `psycopg[binary,pool]` 3 (async) | Plain SQL migrations + a tiny runner |
| Telegram | `python-telegram-bot` v21+ | Polling mode (no public webhook needed) |
| Web UI | FastAPI + Jinja2 + HTMX + Chart.js (both from a CDN) | Server-rendered, no JS build step |
| Scheduler | `APScheduler` (AsyncIOScheduler) | Decision loop tick, nightly jobs |
| Tests | pytest, pytest-asyncio, respx (HTTP mocks), a fake Deriv WS server, Postgres service container | |
| Lint / types | ruff, mypy (strict on `risk/`, `deriv/guard.py`) | |
| Deploy | Docker Compose on a GCP e2-micro (US region, free) + Caddy for HTTPS | 2 GB swap file because RAM is 1 GB |
| CI | GitHub Actions | tests, eval gate, Claude PR review |

---

## 3. Repository layout (created in P1)

```
deriv-agent/
├─ agent/
│  ├─ config.py              # pydantic-settings; every limit lives here
│  ├─ main.py                # wires everything, starts the scheduler
│  ├─ clock.py               # Clock protocol (real + fake) so time is testable
│  ├─ deriv/
│  │  ├─ rest.py             # list accounts, get OTP
│  │  ├─ ws.py               # connection, req_id routing, subscriptions, reconnect
│  │  ├─ guard.py            # demo-only checks (pure functions)
│  │  └─ schemas.py          # Pydantic models for Deriv messages
│  ├─ market/
│  │  ├─ feed.py             # ring buffer per symbol, stale guard
│  │  └─ indicators.py       # returns, volatility, streaks (pure functions)
│  ├─ decision/
│  │  ├─ base.py             # DecisionModel protocol + Decision dataclass
│  │  ├─ systemone.py        # Jev / Clef client (same request shape)
│  │  ├─ claude.py           # Haiku fallback using structured outputs
│  │  ├─ router.py           # primary → fallback, timeouts, provider health
│  │  └─ prompts/decision.yaml   # instructions + criteria (eval-gated in CI)
│  ├─ risk/
│  │  ├─ rules.py            # each rule is a pure function → RuleResult
│  │  └─ gate.py             # runs all rules → APPROVE / REJECT / NEEDS_APPROVAL
│  ├─ execution/executor.py  # re-quote, final guard, buy, track contract
│  ├─ hitl/telegram_bot.py   # approvals, /status /kill /resume /pause etc.
│  ├─ reflection/
│  │  ├─ nightly.py          # builds the day's dataset, calls Haiku
│  │  └─ playbook.py         # validate + apply + version the playbook
│  ├─ reporting/summary.py   # daily Telegram summary
│  ├─ spend.py               # per-provider daily spend caps
│  └─ db/
│     ├─ pool.py, repo.py
│     └─ migrations/001_init.sql …
├─ web/                      # FastAPI dashboard (read-only DB role)
│  ├─ app.py
│  ├─ templates/*.html
│  └─ static/style.css
├─ eval/
│  ├─ build_fixture.py       # windows of stored ticks + what happened next
│  ├─ replay.py              # runs the decision model over the fixture, scores it
│  ├─ fixtures/replay_v1.jsonl
│  └─ thresholds.yaml
├─ tests/
│  ├─ unit/ …
│  ├─ integration/ …
│  └─ fakes/fake_deriv_server.py
├─ sql/metrics.sql           # CV metric queries
├─ scripts/                  # spike scripts, backup, deploy
├─ playbook.md
├─ .claude/                  # hooks + subagents (AI workflow made visible)
│  ├─ settings.json
│  └─ agents/risk-reviewer.md
├─ .github/workflows/{ci.yml,eval.yml,ai-review.yml}
├─ Dockerfile, docker-compose.yml, Caddyfile
├─ .env.example, .gitignore
├─ CLAUDE.md, BREAKLOG.md, README.md, PLAN.md
└─ pyproject.toml
```

---

## 4. Data model (Postgres, all times UTC)

| Table | Key columns | Purpose |
|---|---|---|
| `ticks` | `symbol, epoch, quote, received_at` | Raw market data (keep 14 days) |
| `decisions` | `id, created_at, symbol, provider, model, snapshot jsonb, action, probabilities jsonb, confidence, latency_ms, input_tokens, cost_usd, error` | Every model call, including HOLDs and failures |
| `risk_verdicts` | `decision_id, verdict, reasons text[], rule_results jsonb` | What the gate decided and why |
| `approvals` | `id, decision_id, status (pending/approved/rejected/expired), requested_at, responded_at, tg_message_id` | Human-in-the-loop record |
| `trades` | `id, decision_id, contract_id, contract_type, stake, duration, buy_price, payout, status (open/won/lost/error), profit, bought_at, settled_at, raw jsonb` | Execution + outcome |
| `playbook_revisions` | `id, created_at, content, diff, model, cost_usd, stats jsonb` | Version history of the playbook |
| `agent_events` | `ts, level, kind, message, data jsonb` | Reconnects, stale trips, fallbacks, kills, errors |
| `agent_state` | `key, value, updated_at` | `paused`, `last_heartbeat`, `started_at` |
| `eval_runs` | `id, ts, git_sha, prompt_hash, provider, metrics jsonb, passed` | Eval history (shown on the dashboard) |
| `shadow_decisions` | `decision_round_id, strategy, action, p_call, settled_up bool, paper_profit` | Baseline comparison (§5b) |

Roles: `agent_rw` (agent) and `dashboard_ro` (web, `SELECT` only).

---

## 5. Default configuration (all in `config.py`, overridable via `.env`)

| Setting | Default | Why |
|---|---|---|
| `SYMBOLS` | `1HZ100V` | One symbol keeps things simple; it ticks every second, 24/7 |
| `DECISION_INTERVAL_S` | 60 | Jev is cheap; 1/min gives plenty of data |
| `CONTRACT_TYPE_MAP` | `CALL→CALL`, `PUT→PUT` (Rise/Fall) | **P0 spike must confirm** these exist on the new API; fallback `MULTUP/MULTDOWN` |
| `DURATION` | 5 ticks | Outcome known in about 5 s |
| `STAKE_USD` | 1.00 | Fixed by code |
| `MAX_STAKE_USD` | 2.00 | Hard ceiling, checked again at execution |
| `MAX_OPEN_POSITIONS` | 1 | |
| `DAILY_LOSS_CAP_USD` | 20 | Loss resets at 00:00 UTC |
| `COOLDOWN_AFTER_LOSSES` | 3 losses in a row → 10 min pause | |
| `MIN_CONFIDENCE` | 0.55 | Below this → forced HOLD |
| `APPROVAL_BAND` | 0.55–0.65 | Grey-zone confidence → ask a human |
| `APPROVAL_TIMEOUT_S` | 90 | No answer → expired → not traded |
| `STALE_TICK_S` | 5 | No decision on old data |
| `DAILY_SPEND_CAP_USD` | jev 0.50 / clef 0.50 / anthropic 1.00 | Checked **before** each call |
| `TZ_DISPLAY` | `Asia/Kuala_Lumpur` | Summary 21:00 MYT, reflection 00:30 MYT |

**Human approval is triggered by any of:** confidence in the grey zone · the first trade after `/resume` · the fallback provider was used · daily loss ≥ 50 % of the cap · stake ≠ default.

---

## 5b. Testing and benchmarking strategy (added 2026-10-06)

**Are the trades real?** Yes: real contracts on Deriv's live engine, priced and settled by Deriv, paid for with a **demo account's virtual balance**. Rise/Fall pays less than 2× the stake, so on a random index even a perfect coin-flipper slowly loses. We never judge the project by P&L.

| Layer | What it proves | Where |
|---|---|---|
| Unit + property tests | Each rule and guard is correct; never trades while killed | P1–P4 test tables |
| Integration tests (fake Deriv server) | The full loop works offline, survives disconnects | P1, P2 |
| **Fault-injection suite** (`tests/chaos/`) | The safety layer blocks bad AI output | P2 (built), P5 (run in CI) |
| Replay eval | A prompt/playbook change doesn't make behaviour worse | P5 |
| **Shadow baselines** | Is Jev any better than simple strategies? | P2 (logged), P6 (dashboard page) |
| Engineering metrics | Uptime, latency, cost, blocks, approvals | P7 |

**Fault-injection suite.** A `ChaosProvider` replaces the decision model and returns, in turn: always-CALL at 0.99 confidence · malformed JSON · probabilities that don't sum to 1 · timeouts · an unknown action. Also: a feed that freezes (stale), a patched `/ws/real` URL, a spend cap already reached. Each scenario runs 100× through the real RiskGate/Executor against the fake server, and must give **0 unsafe trades**. The results go to `docs/chaos-results.md`. On the CV these are reported as *injected* tests, separate from live numbers.

**Shadow baselines.** Every decision round, the same snapshot is also scored by three paper strategies (no trades placed, rows only in `shadow_decisions`):

| Baseline | Rule |
|---|---|
| `coin_flip` | random CALL/PUT (seeded) |
| `always_hold` | never trades |
| `momentum_10` | bets the sign of the last 10 ticks' move continues |

Each paper decision is settled against the actual price 5 ticks later, using the payout ratio from the real proposal. The dashboard compares Jev with each baseline on win rate, virtual P&L and Brier score. The expected result on a random index is that nobody beats chance, and the README explains why that's correct.

---

## P0: Accounts, keys and API spike (Tue 10-06 morning, about 2–3 h)

**Goal:** confirm every external dependency actually works before building on it.

**Deliverables:** `.env` filled in (by you), `scripts/spike_*.py`, `docs/api-notes.md` with the real request and response shapes.

**How:** one throwaway script per API that prints raw responses. Anything that turns out different from this plan gets written into `docs/api-notes.md` and the plan is changed *now*, not on day 3.

### TODO
- [ ] 👤 Deriv: sign up or log in → developers.deriv.com dashboard → register an app (**PAT type**) → copy the **App ID**.
- [ ] 👤 Deriv: create a **Personal Access Token** with trading scope for the **demo** account. Put it in `.env` as `DERIV_PAT`, with `DERIV_APP_ID`.
- [ ] 👤 TypeSafe: sign up at console.typesafe.ai → API key → `TYPESAFE_API_KEY`. If you're waitlisted, write it down and move on (the fallback covers it).
- [ ] 👤 Cloudflare: free account → Workers AI API token + account ID → `CF_API_TOKEN`, `CF_ACCOUNT_ID` (fallback for Clef).
- [ ] 👤 Anthropic: Console → API key → `ANTHROPIC_API_KEY`. **Set a monthly spend limit (e.g. $20).**
- [ ] 👤 Telegram: talk to @BotFather → `/newbot` → `TELEGRAM_BOT_TOKEN`. Send your bot a message, then get your `TELEGRAM_CHAT_ID` (Claude will give you a one-line script).
- [ ] 👤 GCP: create a project, enable billing, **add a $1 budget alert**. (The VM gets created in P2.)
- [ ] 👤 GitHub: create an empty **public** repo `deriv-agent`.
- [ ] `scripts/spike_deriv.py`: `POST /trading/v1/options/accounts` (list accounts) → note the response fields that mark demo vs real → `POST /accounts/{id}/otp` → connect → subscribe to `ticks` for `1HZ100V` → print 10 ticks.
- [ ] Same script: send a `proposal` for Rise/Fall (`contract_type: CALL`, `duration: 5`, `duration_unit: t`, `basis: stake`, `amount: 1`, `underlying_symbol: 1HZ100V`). If that's rejected, try `contracts_for` / the multiplier types and record what works.
- [ ] Same script: **buy one 1 USD demo contract**, subscribe to `proposal_open_contract`, print until it's sold or expired.
- [ ] Find out: the OTP's lifetime and whether it's single-use (reconnect with the same URL and see), the ping/keepalive message, and rate-limit errors.
- [ ] `scripts/spike_jev.py`: one `choice` question with the criteria `CALL/PUT/HOLD` on a fake snapshot → print `answers`, `confidence`, `usage`, latency.
- [ ] `scripts/spike_clef.py`: same request to Cloudflare Clef-flash → confirm the shape matches.
- [ ] `scripts/spike_claude.py`: Haiku 4.5 structured output that returns the same shape.
- [ ] `scripts/spike_telegram.py`: send "hello" with two inline buttons → print the callback when you press one.
- [ ] Write `docs/api-notes.md` (real field names, quirks, open questions).
- [ ] Start `BREAKLOG.md` with the template (see appendix B). Log anything that surprised us.

### Unit tests
None. This phase is exploration. The captured responses are saved to `tests/fixtures/deriv/*.json` and `tests/fixtures/systemone/*.json` and used later as test data.

### Exit criteria
- [ ] One demo contract bought and settled from a script.
- [ ] At least one decision provider (Jev **or** Clef **or** Haiku) returns a valid answer.
- [ ] Telegram button callback received.
- [ ] We know for certain which field marks an account as demo.

**Cut if behind:** Cloudflare (keep only Jev + Haiku).

---

## P1: Foundation and market feed (Tue 10-06)

**Goal:** a clean repo where live ticks flow into Postgres reliably and old data is detected.

**Deliverables:** repo skeleton, `CLAUDE.md`, `docker-compose.yml` (agent + postgres), migration `001_init.sql`, `DerivClient` with reconnect, `MarketFeed` with stale guard, CI running lint + tests.

### How
- **Connection management (`deriv/ws.py`):**
  - Every request gets a `req_id`. Responses are routed to the matching `asyncio.Future`, and subscription messages are routed to a per-subscription queue.
  - Reconnect loop: on any close or error → backoff `min(30, 2^n) s + jitter` → **fetch a fresh OTP** → reconnect → **resubscribe** to everything that was active → log an `agent_event`.
  - Watchdog: if no message arrives for 15 s, force a reconnect. Send the keepalive found in P0 every 30 s.
- **Demo guard (`deriv/guard.py`, pure functions, written here even though trading starts in P2):**
  - `assert_demo_ws_url(url)`: the path must end with `/ws/demo`, otherwise `RealAccountRefused`.
  - `assert_demo_account(account_json)`: uses the demo field found in P0.
- **Market feed (`market/feed.py`):**
  - A ring buffer of the last N = 600 ticks per symbol.
  - `snapshot(symbol, now)` raises `StaleData` if `now - last_tick.received_at > STALE_TICK_S`, or if fewer than 120 ticks are buffered.
  - Time comes from the `Clock` protocol so tests can control it.
- **Tick storage:** batch inserts (every 2 s or 50 ticks) so we don't do one INSERT per tick on a 1 GB VM.
- **Migrations:** `agent/db/migrations/NNN_*.sql`, applied at startup by a runner that records them in `schema_migrations`.
- **CLAUDE.md** has: the rules from §1, the layout, how to run tests (`pytest -q`), "never edit `risk/` without a test", and commit style.
- **Claude Code hooks** (`.claude/settings.json`): after editing a `*.py` file, run `ruff check` plus the matching test file. Commit this, because it's evidence of the AI-first workflow.

### TODO
- [ ] `git init`, `pyproject.toml` (deps + ruff + mypy + pytest config), `.gitignore` (`.env`, `__pycache__`, `backups/`).
- [ ] `.env.example` with every variable from P0 (names only).
- [ ] `CLAUDE.md`, `BREAKLOG.md`, empty `playbook.md` with a header.
- [ ] `Dockerfile` (python:3.12-slim, non-root user) and `docker-compose.yml`: `postgres` (with a healthcheck and a volume), `agent` (`restart: unless-stopped`, `depends_on: postgres healthy`).
- [ ] `001_init.sql`: every table in §4, plus indexes on `ticks(symbol, epoch)` and `decisions(created_at)`, plus both roles.
- [ ] `agent/config.py` with every setting from §5, validated (e.g. `MIN_CONFIDENCE` must be between 0.5 and 1).
- [ ] `agent/clock.py`: `SystemClock`, `FakeClock`.
- [ ] `agent/deriv/rest.py`: `list_accounts()`, `get_ws_url(account_id)`.
- [ ] `agent/deriv/guard.py` + tests.
- [ ] `agent/deriv/ws.py`: connect, send/await by `req_id`, subscribe, forget, reconnect, watchdog.
- [ ] `tests/fakes/fake_deriv_server.py`: a local `websockets` server that streams ticks, can drop the connection when told to, and answers `proposal` / `buy` from fixtures.
- [ ] `agent/market/feed.py` + `indicators.py` (log returns, rolling volatility over 30/120 ticks, up/down streak, distance from the 60-tick mean).
- [ ] Tick persister (batched).
- [ ] `agent/main.py`: start the DB pool → run migrations → DerivClient → subscribe → persist. Heartbeat to `agent_state` every 10 s.
- [ ] `.claude/settings.json` hook + `.claude/agents/risk-reviewer.md` (a subagent that reviews any diff touching `risk/` or `deriv/guard.py` against the rules).
- [ ] `.github/workflows/ci.yml`: ruff, mypy, pytest with a Postgres service.
- [ ] First push to GitHub. Small commits from here on.
- [ ] Run locally with `docker compose up` for **1 hour**. Kill the network once (turn Wi-Fi off for 30 s) and check that it reconnects.

### Unit tests (`tests/unit/`)
| Test | Checks |
|---|---|
| `test_guard_accepts_demo_url` | `…/ws/demo?otp=x` passes |
| `test_guard_rejects_real_url` | `…/ws/real?otp=x` → `RealAccountRefused` |
| `test_guard_rejects_unknown_path` | anything else → refused (fail closed) |
| `test_guard_rejects_real_account_json` | fixture of a real account → refused |
| `test_feed_snapshot_fresh` | ticks 1 s old → snapshot returned |
| `test_feed_snapshot_stale` | `FakeClock` moved 6 s forward → `StaleData` |
| `test_feed_snapshot_insufficient_history` | < 120 ticks → `StaleData` |
| `test_feed_ring_buffer_caps` | 1,000 ticks pushed → only the last 600 kept |
| `test_indicators_known_values` | hand-computed volatility / streak on a tiny series |
| `test_config_rejects_bad_limits` | `MIN_CONFIDENCE=0.3` → validation error |
| `test_backoff_sequence` | 1, 2, 4, 8, 16, 30, 30 … (jitter mocked) |

### Integration tests (`tests/integration/`)
| Test | Checks |
|---|---|
| `test_ws_reconnect_and_resubscribe` | the fake server drops the connection → client reconnects with a **new** OTP and receives ticks again |
| `test_ws_watchdog_triggers` | the fake server goes silent 20 s (fake clock) → reconnect |
| `test_req_id_routing` | two parallel requests get their own responses |
| `test_migrations_idempotent` | running the migrations twice → no error |
| `test_tick_persist_batch` | 120 ticks → 120 rows, ≤ 5 INSERT statements |

### Exit criteria
- [ ] All tests green in CI.
- [ ] Ticks stream into local Postgres for **1 h** with no crash. Count check: rows ≈ 3,600 ± 5 %.
- [ ] The forced disconnect recovered automatically, and an `agent_event` row shows it.
- [ ] The first `BREAKLOG.md` entry exists (there will be one).

**Cut if behind:** mypy strictness (keep ruff), the watchdog (keep reconnect).

---

## P2: Decide → Risk → Execute, then deploy (Wed 10-07)

**Goal:** the full autonomous loop places demo trades **from the cloud VM**.

**Deliverables:** `DecisionRouter` (Jev → Clef → Haiku), `RiskGate`, `Executor`, spend caps, the decision loop, running on GCP.

### How
- **The decision request (SystemOne format):**
  - `state`: an object with the latest indicators, the last 60 ticks (rounded), the open position, today's P&L, and the **current playbook text**.
  - `questions.action`: `type: choice`, `instructions` from `prompts/decision.yaml`, `criteria`: `{CALL: "price likely higher in 5 ticks", PUT: "…lower…", HOLD: "no clear read / conditions unsafe"}`.
  - Optional `questions.regime`: `type: score`, 3 levels (calm / normal / chaotic), logged for analysis.
- **Router (`decision/router.py`):**
  - Try the providers in order, each with a 3 s timeout.
  - Each provider has a small circuit breaker: 3 failures → skip it for 5 min.
  - The spend cap is checked before every call.
  - The result is a `Decision(action, probabilities, confidence, provider, latency_ms, cost_usd)`.
  - Errors → `Decision(action=HOLD, error=…)`. **Something always gets logged.**
- **Haiku fallback:** structured output with a JSON schema that matches the same `Decision` fields. Its probabilities are marked `calibrated=false`.
- **RiskGate (`risk/`):**
  - Each rule is `def rule(ctx) -> RuleResult(ok: bool, reason: str, needs_approval: bool)`.
  - Order:
    1. `kill_switch` (env + DB)
    2. `paused`
    3. `demo_only`
    4. `action_is_trade` (HOLD → no-op)
    5. `confidence_floor`
    6. `stake_within_max`
    7. `max_open_positions`
    8. `daily_loss_cap`
    9. `cooldown_after_losses`
    10. `data_fresh`
    11. `approval_triggers`
  - Any `ok=False` → REJECT. Any `needs_approval` → NEEDS_APPROVAL. Otherwise APPROVE.
  - No I/O inside the rules. `ctx` is built beforehand, which makes the rules trivial to test.
- **Executor (`execution/executor.py`):**
  1. `assert_demo_ws_url` (again).
  2. Fetch a fresh `proposal`.
  3. **Re-run RiskGate** with the fresh context (state may have changed).
  4. `buy` with `price` = the proposal's ask price (rejects slippage).
  5. Store the trade.
  6. Subscribe to `proposal_open_contract` until it settles.
  7. Update the trade and the loss counters.
  - Idempotency: a unique `decision_id` on `trades` means one decision can never buy twice.
- **Decision loop:** APScheduler every `DECISION_INTERVAL_S`:
  - If paused or killed, log and skip.
  - Otherwise: snapshot (`StaleData` → log + skip) → decide → gate → (P2: NEEDS_APPROVAL is treated as REJECT until Telegram exists in P3) → execute.
- **Deployment (GCP e2-micro):**
  - Region `us-central1` (free tier), Debian 12, 30 GB standard disk.
  - Install Docker and the compose plugin. **Create a 2 GB swap file.** Tune Postgres: `shared_buffers=64MB`, `max_connections=20`.
  - Clone the repo, copy `.env` with `scp` (never commit it), `docker compose up -d`.
  - `scripts/deploy.sh`: `git pull && docker compose up -d --build && docker compose ps`.

### TODO
- [ ] `decision/base.py` (protocol, `Decision`, `Snapshot` → `state` serializer).
- [ ] `prompts/decision.yaml` v1. Store its hash in each decision row.
- [ ] `decision/systemone.py` (base URL + model + key are configurable, so Jev and Clef share one class).
- [ ] `decision/claude.py` (Haiku, structured output).
- [ ] `decision/router.py` + circuit breaker + timeouts.
- [ ] `spend.py`: estimate cost from `usage` (Jev $0.042/M input; Clef-flash $0.09/M; Haiku $1/$5 per M) → daily sum per provider → refuse when over the cap.
- [ ] `risk/rules.py` + `risk/gate.py`. **Have the risk-reviewer subagent review them before committing.**
- [ ] `execution/executor.py`.
- [ ] Decision loop in `main.py`.
- [ ] Persist every step: `decisions` → `risk_verdicts` → `trades`.
- [ ] Run against the fake server in an integration test, then **locally against the real demo** for 30 min.
- [ ] 👤 Create the GCP VM (Claude gives exact click-by-click or `gcloud` commands).
- [ ] Provision the VM: Docker, swap, firewall (SSH only for now), clone, `.env`, `docker compose up -d`.
- [ ] Watch the logs until the **first autonomous trade from the VM**. Screenshot it for the README.
- [ ] Nightly `pg_dump` cron → `~/backups` (keep 7). The CV metrics depend on this data.
- [ ] Turn on the GCP VM uptime check or set a cron that checks the heartbeat (simple version: Telegram alert in P3).

### Unit tests
| Test | Checks |
|---|---|
| `test_rule_kill_switch_blocks_everything` | killed → REJECT for every action/confidence (parametrised) |
| `test_rule_confidence_floor` | 0.54 → REJECT reason `confidence_floor`; 0.56 → passes |
| `test_rule_stake_cap` | stake 2.01 → REJECT |
| `test_rule_max_open_positions` | 1 open → REJECT |
| `test_rule_daily_loss_cap` | P&L −20.00 → REJECT; −19.99 → passes |
| `test_rule_cooldown` | 3 losses in a row, 5 min ago → REJECT; 11 min ago → passes |
| `test_rule_stale_data` | snapshot age 6 s → REJECT |
| `test_gate_hold_is_noop` | HOLD → verdict `NO_TRADE`, executor never called |
| `test_gate_grey_zone_needs_approval` | confidence 0.60 → NEEDS_APPROVAL |
| `test_gate_fallback_provider_needs_approval` | provider = haiku → NEEDS_APPROVAL |
| `test_gate_property_never_approves_when_killed` | Hypothesis: random contexts with kill=on → never APPROVE |
| `test_systemone_parses_answer` | recorded fixture → `Decision` with probabilities that sum to about 1 |
| `test_systemone_bad_payload_becomes_hold` | junk JSON → HOLD + error logged |
| `test_router_falls_back_on_timeout` | Jev times out (respx) → Clef used |
| `test_router_circuit_breaker` | 3 failures → provider skipped for 5 min (fake clock) |
| `test_router_all_fail_returns_hold` | every provider down → HOLD |
| `test_spend_cap_blocks_call` | today's spend ≥ cap → provider not called |
| `test_executor_rechecks_risk_after_requote` | state changes between gate and buy → no buy |
| `test_executor_refuses_real_url` | patched URL `/ws/real` → `RealAccountRefused`, no `buy` sent |
| `test_executor_idempotent` | same `decision_id` twice → one buy |

### Integration tests
| Test | Checks |
|---|---|
| `test_full_loop_fake_server` | fake ticks → decision (mocked Jev) → gate → buy → settle → rows in all 3 tables |
| `test_loop_skips_on_stale` | fake server stops ticking → decision rows with `StaleData`, no trades |

### Exit criteria
- [ ] Every risk rule has a passing test, including the property test.
- [ ] **The first autonomous demo trade is placed from the VM** and settled, and the DB rows link decision → verdict → trade.
- [ ] The agent survives `docker compose restart agent` and a VM reboot (restart policy works).
- [ ] The daily backup file exists.

**Cut if behind:** Clef provider (Jev → Haiku only), the circuit breaker (keep timeouts), the `regime` question.

---

## P3: Telegram human-in-the-loop and outcomes (Thu 10-08)

**Goal:** you can watch, approve, pause and kill the agent from your phone.

**Deliverables:** Telegram bot with approvals and commands, the daily summary, alerts.

### How
- **Bot runs inside the agent process**, using `python-telegram-bot` polling in the same event loop. Every handler first checks `chat_id == TELEGRAM_CHAT_ID`; messages from anyone else are ignored and logged.
- **Approval flow:**
  - NEEDS_APPROVAL → insert an `approvals` row (pending) → send a card with ✅ Approve / ❌ Reject buttons.
  - The `callback_data` includes the approval id plus a short HMAC, so it can't be forged.
  - Approve → the executor runs (which re-quotes and re-runs the risk check).
  - Reject → recorded.
  - A scheduler job expires approvals older than `APPROVAL_TIMEOUT_S` and edits the card to show "⌛ expired".
- **Kill vs pause:**
  - `/pause` stops new decisions; open contracts settle normally.
  - `/kill` sets `paused` + `killed`. It also tries to sell open positions early if the contract type allows it; otherwise it lets them expire. Each attempt is logged.
  - `/resume` clears both. The first trade after `/resume` needs approval.
- **Alerts (pushed, not requested):** agent start/restart · provider fallback switched on · daily loss cap hit · 3+ reconnects in 10 min · no heartbeat for 2 min (sent by a tiny cron on the VM that checks `agent_state`).
- **Daily summary (21:00 MYT):** SQL aggregates → Haiku turns them into 5 lines → sent. If Haiku is unavailable or over budget, send the raw numbers.

### Telegram UI spec
```
🟡 Approval needed  #A-142
Symbol   1HZ100V      Action  CALL (Rise)
Stake    $1.00        Duration 5 ticks
Model    jev-1.13     Confidence 0.61
P(CALL/PUT/HOLD)  0.61 / 0.27 / 0.12
Why asked: confidence in grey zone (0.55–0.65)
Today: 23 trades · P&L −$2.40 · loss cap $20
[ ✅ Approve ]  [ ❌ Reject ]          expires in 90 s
```
| Command | Reply |
|---|---|
| `/status` | state (running / paused / killed) · uptime · last tick age · provider in use · open position · today: decisions / trades / W-L / P&L / spend |
| `/pause`, `/resume`, `/kill` | confirmation + new state |
| `/last 5` | the last 5 decisions, one line each (time · action · conf · verdict · result) |
| `/limits` | current risk config |
| `/playbook` | current playbook (first 3,500 characters) + revision number |
| `/help` | list of commands |

### TODO
- [ ] `hitl/telegram_bot.py`: auth check, commands, callback handler with HMAC.
- [ ] Approval lifecycle + expiry job + editing the card on approve/reject/expire.
- [ ] Wire NEEDS_APPROVAL into the loop (replacing P2's treat-as-reject).
- [ ] `/kill` early-sell attempt (if the contract type supports `sell`).
- [ ] Alerts listed above.
- [ ] `reporting/summary.py` + 21:00 MYT job.
- [ ] Heartbeat-watchdog cron on the VM (`scripts/watchdog.sh`, sends via the Bot API using `curl`).
- [ ] Deploy. Test on your phone: approve one, reject one, let one expire, `/kill`, `/resume`.

### Unit tests
| Test | Checks |
|---|---|
| `test_bot_ignores_other_chats` | an update from another chat_id → no action, warning logged |
| `test_callback_hmac_rejects_tampered` | changed approval id → refused |
| `test_approval_expires` | fake clock +91 s → status `expired`, executor not called |
| `test_approve_triggers_executor_with_requote` | approve → executor called and RiskGate called again |
| `test_double_click_approve_is_idempotent` | two callbacks → one trade |
| `test_kill_sets_flags_and_blocks_loop` | `/kill` → next loop tick doesn't call the model |
| `test_resume_requires_first_trade_approval` | after `/resume`, the next APPROVE turns into NEEDS_APPROVAL |
| `test_status_formatting` | known DB state → expected text (snapshot test) |
| `test_summary_fallback_without_llm` | Haiku raises → raw-numbers summary still sent |

### Exit criteria
- [ ] Done on a real phone: approve → trade happens; reject → no trade; ignore → expires; `/kill` → stops within one interval; `/resume` → continues.
- [ ] First daily summary received at 21:00 MYT.
- [ ] Stopping the agent container triggers the heartbeat alert within 2–3 min.

**Cut if behind:** `/last`, `/playbook`, early-sell on kill.

---

## P4: Nightly reflection and web dashboard v1 (Fri 10-09)

**Goal:** the agent improves its own playbook every night, and anyone with the link can watch it live.

### Part A: Nightly reflection

**How:**
- **00:30 MYT job:**
  - Build a dataset: the last 24 h of decisions joined with outcomes, grouped by confidence bucket, regime, provider and indicator ranges.
  - Calculate stats **in SQL/Python**, not in the LLM: win rate per bucket, HOLD rate, rejections by reason.
  - Send Haiku: the current playbook + the stats table + 20 sample decisions.
- **Haiku returns structured output:** `{lessons: [{text, evidence, confidence}], remove: [lesson_ids], summary}`.
- **`playbook.py` validates before applying:**
  - At most 5 new lessons per night.
  - Each lesson ≤ 280 characters.
  - The whole playbook ≤ 6,000 characters (the oldest low-confidence lessons get evicted).
  - **Blocklist:** a lesson may not mention stake, limits, loss cap, approval or kill. Risk parameters can't be changed by the playbook.
  - Every lesson must cite evidence (a stat from the table).
- Store a revision (`content`, unified `diff`, `stats`, `cost`) in `playbook_revisions`, write `playbook.md`, and send the diff to Telegram.
- **Rollback:** `/playbook rollback <rev>`.
- **Honesty guard:** if there are fewer than 30 settled trades that day, skip the revision and log "insufficient data". Don't invent lessons from noise.

**TODO**
- [ ] `reflection/nightly.py` (dataset + stats + Haiku call).
- [ ] `reflection/playbook.py` (validate → apply → version → diff).
- [ ] Telegram: send the revision diff, `/playbook rollback`.
- [ ] Run it once by hand on today's data. Review the output yourself.

**Unit tests**
| Test | Checks |
|---|---|
| `test_playbook_rejects_risk_keywords` | a lesson with "increase stake" → dropped |
| `test_playbook_caps_size_and_count` | 9 lessons → 5 kept; overflow evicts the oldest low-confidence ones |
| `test_playbook_requires_evidence` | a lesson without evidence → dropped |
| `test_revision_diff_and_rollback` | apply → diff stored; rollback restores the exact earlier content |
| `test_reflection_skips_low_data` | 12 trades → no revision, event logged |
| `test_stats_computation` | known rows → expected win rate per bucket |

### Part B: Web dashboard v1 (see §UI plan for the full spec)

**How:**
- A `web` service in compose: FastAPI + Jinja2 + HTMX. It connects as **`dashboard_ro`**.
- No write routes and no secrets on any page.
- HTMX polls fragments every 5–10 s.
- Caddy service in front, for automatic HTTPS on `<vm-ip>.sslip.io` (or your own domain).
- Open firewall ports 80/443 only.

**TODO**
- [ ] `web/app.py` routes: `/` (Overview), `/decisions`, `/decisions/{id}`, `/health` (JSON).
- [ ] Templates + one CSS file (light/dark), mobile-friendly.
- [ ] `Caddyfile`, firewall rule, deploy.
- [ ] Banner on every page: "Demo account, virtual money. Not financial advice. Research project."

**Unit tests**
| Test | Checks |
|---|---|
| `test_web_overview_renders` | seeded DB → 200, KPI values present |
| `test_web_no_secret_leak` | every page response checked for every value in `.env` → none found |
| `test_web_readonly_role` | `dashboard_ro` INSERT → permission denied |
| `test_web_health_json` | `/health` → `{status, last_tick_age_s, last_decision_at}` |
| `test_web_decision_detail_404` | unknown id → 404, not 500 |

### Exit criteria
- [ ] **The first automatic playbook revision** was created by the scheduled job (not run by hand), with a diff stored and sent to Telegram.
- [ ] The dashboard is reachable over HTTPS from your phone on mobile data.
- [ ] The secret-leak test passes.

**Cut if behind:** rollback command (do it with manual SQL), the `/decisions` filter UI.

---

## P5: Replay eval, CI gate and AI review (Sat 10-10)

**Goal:** a prompt or playbook change can't ship if it makes the agent's behaviour worse.

### How
- **Fixture (`eval/build_fixture.py`):** sample 200 windows from the stored `ticks` (spread across days and volatility regimes). Each window is a snapshot plus what happened in the next 5 ticks (up/down). Save as `replay_v1.jsonl`, committed.
- **Replay (`eval/replay.py`):** run the current `prompts/decision.yaml` + `playbook.md` through the provider over every window. Run the real RiskGate on each answer. Metrics:
  | Metric | Meaning | Gate |
  |---|---|---|
  | `schema_valid_rate` | answers parsed into a valid `Decision` | = 1.00 |
  | `risk_violation_rate` | would-be trades the gate rejects for *model-caused* reasons (e.g. confidence-floor abuse) | ≤ baseline + 0.05 |
  | `hold_rate` | share of HOLD | 0.10 ≤ x ≤ 0.90 (the agent mustn't collapse to always-trade or never-trade) |
  | `action_entropy` | diversity of CALL/PUT | ≥ 0.5 bits on non-HOLD answers |
  | `brier_score` | calibration of P(CALL) vs actual up | ≤ baseline + 0.02 |
  | `directional_acc` | reported only, **not gated** | expected ≈ 0.50 (random index; README explains) |
- **Baseline:** `eval/baseline.json` comes from the current main branch. A PR fails if any gate fails versus the baseline.
- **CI:**
  - `eval.yml` runs on PRs that touch `agent/decision/**`, `playbook.md` or `eval/**`, plus on manual trigger.
  - It uses the `TYPESAFE_API_KEY` secret. On forks without secrets → skipped with a notice.
  - Results are posted as a PR comment and written to `eval_runs` (via an artifact the agent imports, or skipped in CI).
  - Cost per run: about 200 × 2k tokens × $0.042/M ≈ $0.02.
- **The "deliberately worse prompt" demo:** a PR that changes the instructions to *"Always choose CALL"*. Expected result: `hold_rate` → 0 and `action_entropy` → 0, so CI goes red. Keep this PR open or closed with screenshots as evidence.
- **AI code review:** `ai-review.yml` uses Anthropic's Claude Code GitHub Action (verify the current action name/version in its docs) to review every PR. A repo-level instruction file tells it to focus on the risk rules and the demo guard.

### TODO
- [ ] `eval/build_fixture.py` → `replay_v1.jsonl` (200 windows).
- [ ] `eval/replay.py` + `thresholds.yaml` + `baseline.json`.
- [ ] `eval.yml` workflow + PR comment step.
- [ ] `ai-review.yml` workflow + review instructions.
- [ ] 👤 Add the GitHub secrets: `TYPESAFE_API_KEY`, `ANTHROPIC_API_KEY`.
- [ ] Open PR "bad prompt: always CALL" → confirm CI blocks it → screenshot.
- [ ] Open PR with a small genuine prompt tweak → CI passes → merge.

### Unit tests
| Test | Checks |
|---|---|
| `test_metrics_hold_rate` / `_entropy` / `_brier` | hand-computed values on tiny inputs |
| `test_gate_fails_always_call` | synthetic all-CALL answers → gate fails on entropy + hold rate |
| `test_gate_passes_balanced` | a balanced synthetic set → passes |
| `test_fixture_has_no_lookahead` | a window's snapshot never includes ticks from its outcome period |
| `test_replay_uses_real_riskgate` | the replay imports `risk.gate`, not a copy |

### Exit criteria
- [ ] **CI blocks the "always CALL" PR** (red check, screenshot saved).
- [ ] The AI review comment appears on a PR.
- [ ] Eval history is visible (PR comments now; the dashboard Evals page in P6).

**Cut if behind:** the Brier gate (keep schema + hold + entropy), writing `eval_runs` from CI.

---

## P6: Polish (UI, README, video, hardening) (Sun 10-11)

**Goal:** a stranger can understand it in 2 minutes and run it in 10.

### TODO
- [ ] Dashboard v2 pages: **Risk**, **Playbook**, **Evals**, **Costs**, **Incidents** (see the UI plan).
- [ ] README:
  - one-paragraph pitch
  - live dashboard link
  - architecture diagram (Mermaid)
  - "Safety model" section (the rules, how the demo guard works, the human-in-the-loop flow)
  - "Why direction accuracy is ~50 % and why that's fine"
  - how to run locally (5 commands)
  - how we build with Claude Code (hooks, subagent, CI review)
  - "what I'd build next"
- [ ] Clean up `BREAKLOG.md`: every entry has all 4 fields; pick the best incident for the application note.
- [ ] Hardening pass:
  - Postgres retention job (ticks > 14 days deleted)
  - log rotation (`docker` json-file `max-size`)
  - check disk usage
  - `docker compose` resource limits
- [ ] Load check: `free -m` and `docker stats` on the VM. Stay under ~80 % RAM with swap rarely used.
- [ ] Record a **2-minute demo video**:
  1. dashboard overview
  2. a live decision appears
  3. a Telegram approval card → approve
  4. the trade settles on the dashboard
  5. `/kill` → the dashboard shows KILLED
  6. the playbook revision diff
  7. the red CI on the bad-prompt PR
  8. a BREAKLOG incident
- [ ] Fresh-clone test: clone into a new folder, follow the README, and confirm it runs against the fake server with `make demo` (no keys needed).

### Unit tests
- [ ] `test_retention_deletes_old_ticks_only`
- [ ] `test_web_pages_render` (parametrised over every route)
- [ ] `make demo` smoke test in CI (fake server + mocked providers, runs for 60 s, asserts ≥ 1 trade row)

### Exit criteria
- [ ] Someone other than you (or a fresh clone) gets it running using only the README.
- [ ] The video is uploaded (unlisted YouTube or Loom) and linked in the README.
- [ ] The agent has been running on the VM since P2 with only expected restarts.

**Cut if behind:** Costs and Incidents pages (fold them into Overview), `make demo` in CI.

---

## P7: Metrics, CV and submission (Mon 10-12)

**Goal:** submit with real, verifiable numbers.

### TODO
- [ ] Run `sql/metrics.sql` on the VM and paste the results into `docs/metrics-2026-10-12.md`:
  - days of uptime (first heartbeat → now, minus gaps longer than 5 min)
  - total decisions; HOLD / CALL / PUT split
  - risk-gate verdicts by reason (**"blocked N unsafe actions"** comes from here)
  - approvals requested, approve/reject/expire ratio
  - trades placed and settled (W/L shown, but **no profitability claims**)
  - playbook revisions
  - model calls per provider, fallback activations
  - p50/p95 decision latency, total spend per provider
  - reconnects survived, stale-data skips
- [ ] Fill in the CV bullets (`deriv-agent-HANDOFF.md` §8) **only with numbers from this file**. Delete any bullet that isn't true.
- [ ] Write the application note (4–6 sentences) from the chosen BREAKLOG incident: what it is → built in a week with Claude Code (one workflow detail) → what broke → the fix + the general lesson.
- [ ] Final check: dashboard link works logged-out on mobile; GitHub repo is public; no secrets in git history (`git log -p | grep` for key prefixes); video link works.
- [ ] 👤 **Submit the application.**
- [ ] Leave the agent running. Set a calendar reminder to check it twice a week until interviews are done.

### Exit criteria
- [ ] Application sent, and every number on the CV traces back to `docs/metrics-*.md`.

---

## UI plan

Two interfaces with separate jobs:

| | **Telegram** (operator console) | **Web dashboard** (public window) |
|---|---|---|
| Who | You (one allowed chat) | Recruiters, interviewers, you |
| Can change things? | Yes: approve, pause, kill, rollback | **No, read-only** |
| Built in | P3 | P4 (v1), P6 (v2) |

Telegram details are in P3. Everything below is about the dashboard.

### Design principles
- **Show proof, not hype.** Every number links to the rows behind it. The "demo account / not advice" banner is always visible.
- **Make it obviously alive.** A heartbeat dot and "last tick 0.8 s ago" are the first things you see.
- **Make safety visible.** Rejections, approvals and kills are shown as prominently as trades.
- Server-rendered, fast on mobile, light and dark themes, no login, no write paths.

### Information architecture
```
Top bar:  ● LIVE (or ● PAUSED / ● KILLED / ● STALE)  |  Overview  Decisions  Risk  Playbook  Evals  Costs  Incidents  |  GitHub ↗
Banner:   Demo account · virtual money · research project, not financial advice
```

### Pages

**1. Overview** `/` (v1)
```
┌───────────────────────────────────────────────────────────────────┐
│ ● LIVE · up 4d 06h · last tick 0.8s ago · provider: jev-1.13        │
├──────────┬──────────┬──────────┬──────────┬──────────┬─────────────┤
│Decisions │ Trades   │ Blocked  │Approvals │ Playbook │ Spend today │
│  today   │  today   │ by risk  │ asked    │ revision │             │
│  1,204   │   61     │   212    │  9 (7✅) │   #4     │  $0.06      │
├──────────┴──────────┴──────────┴──────────┴──────────┴─────────────┤
│ Price (1HZ100V, last 10 min) with trade markers ▲CALL ▼PUT ✕blocked │
│ [ Chart.js line chart ]                                             │
├───────────────────────────────────────────────────────────────────┤
│ Live decision feed (HTMX, refresh 5s)                               │
│ 14:02:11  CALL 0.71  ✅ approved by gate  → WON  +$0.95            │
│ 14:01:11  HOLD 0.48  —                                              │
│ 14:00:11  PUT  0.60  🟡 asked human → approved → LOST −$1.00        │
│ 13:59:11  CALL 0.66  ⛔ blocked: cooldown_after_losses              │
└───────────────────────────────────────────────────────────────────┘
```

**2. Decisions** `/decisions` (v1) + detail `/decisions/{id}`
- Table: time · action · confidence · provider · verdict · reason · outcome · latency. Filters: verdict, provider, date.
- **Detail page (the explainability page):**
  - the exact `state` the model saw (indicators + playbook revision #)
  - probability bars for CALL/PUT/HOLD
  - each risk rule with ✅/⛔ and its reason
  - approval timeline (requested → answered by you, or expired)
  - the contract (buy price, payout, result)
  - latency and cost

**3. Risk** `/risk` (v2)
- Bar chart of rejections by reason (all time / today).
- Approval funnel: asked → approved / rejected / expired.
- Kill/pause timeline.
- Current limits (read from config).

**4. Playbook** `/playbook` (v2)
- Current playbook (rendered markdown).
- Revision list: date · lessons added/removed · trades analysed.
- Click a revision → coloured diff + the stats table that justified it.

**5. Evals** `/evals` (v2)
- Table of eval runs: commit · prompt hash · each metric · pass/fail.
- A line chart of `hold_rate` and `brier_score` over time.
- Link to the red "always CALL" PR.

**6. Costs & latency** `/costs` (v2)
- Spend per provider per day (stacked bars) against the caps.
- p50/p95 latency per provider.
- Calls per provider and fallback activations.

**7. Incidents** `/incidents` (v2)
- `agent_events` with level ≥ warning (reconnects, stale trips, fallbacks, kills).
- The rendered `BREAKLOG.md` below them: "what broke and what I learned", the story behind the application note.

**8. `/health`** (JSON, v1): used by the uptime check and linked in the README.

### Visual style
- Neutral greys plus one accent colour. Status colours: green = trade won / live, red = lost / killed, amber = needs approval / stale, slate = HOLD / blocked.
- Monospace numbers and right-aligned columns in tables.
- A 16 px side gutter on mobile; KPI cards wrap 2-up on phones.
- Every chart has a text alternative (the table below it).

### Dashboard TODO summary
- v1 (P4): Overview, Decisions + detail, `/health`, banner, Caddy HTTPS, read-only role, secret-leak test.
- v2 (P6): Risk, Playbook + diffs, Evals, Costs, Incidents, dark mode polish, mobile check.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Jev access is waitlisted | Medium | Medium | Clef + Haiku fallbacks behind the same interface (P2) |
| New Deriv API differs from the docs (contract types, OTP rules) | Medium | High | P0 spike before any real code; `docs/api-notes.md` |
| 1 GB VM runs out of memory | Medium | High | Swap, small Postgres settings, batched inserts, resource limits, `docker stats` check |
| VM IP changes, so the dashboard link breaks | Low | Medium | Don't stop the VM; if needed, buy a cheap domain or reserve a static IP (small cost) |
| Agent dies silently overnight | Medium | High | `restart: unless-stopped`, heartbeat watchdog → Telegram alert |
| Overspending on APIs | Low | Low | Per-provider daily caps in code + Anthropic Console limit + GCP budget alert |
| Running behind schedule | High | High | "Cut if behind" in each phase; **deployment on Wed is non-negotiable** |
| Secret leaked to git | Low | High | `.gitignore`, secret-leak test, pre-push scan of `git log -p` |
| Overclaiming on the CV | Low | High | P7: every number comes from `metrics.sql` output saved in the repo |

## Cut order if the week slips (cut from the top)
1. Dashboard v2 pages other than Playbook
2. Cloudflare Clef fallback
3. Brier-score gate
4. `/last`, `/playbook`, rollback commands
5. AI PR review workflow
6. Demo video (replace it with README screenshots)

**Never cut:** demo guard, RiskGate + its tests, deployment from Wed, Telegram kill switch, decision log, BREAKLOG.

---

## Appendix A: Definition of done (every task)
- Code has type hints and passes ruff.
- New behaviour has a test; risk/guard changes have the risk-reviewer subagent's sign-off.
- Committed with a clear message; CI is green.
- If it broke anything on the way, there's a BREAKLOG entry.
- Deployed to the VM if it affects runtime.

## Appendix B: BREAKLOG entry template
```
## YYYY-MM-DD HH:MM MYT: <short title>
**What broke:**
**How it was found:** (alert / test / dashboard / Telegram / by eye)
**Root cause:**
**Fix:** (commit link)
**Lesson:** (one general sentence)
```

## Appendix C: Daily operations checklist (from P2 onward)
- [ ] Morning: `/status` on Telegram, dashboard heartbeat green, check last night's reflection diff.
- [ ] Check the disk (`df -h`) and memory (`free -m`) on the VM.
- [ ] Skim incidents; add BREAKLOG entries.
- [ ] Evening: read the 21:00 summary.
