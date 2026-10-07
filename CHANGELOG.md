# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Dashboard** (`web/`, http://localhost:8080): live status, decision pipeline, KPIs, price chart with trade markers, decision feed with per-decision explainability page, and model-vs-baselines comparison. Read-only; no CDN dependencies.
- **Paper mode** (default): live ticks and real quotes from Deriv's public endpoint, every safety check unchanged, fills simulated at the quoted price and settled on live ticks. Added because Deriv's trading API isn't offered to Malaysian residents.
- Labelled rule-based stand-in decision model for running without an AI key (paper mode only).
- `agent.loop`: autonomous decision rounds (snapshot → model → log → shadow baselines → RiskGate → executor), every round logged including skips and HOLDs.
- `agent.shadow`: paper baselines (coin flip, always hold, momentum) plus the model's own direction, settled on real exit ticks.
- `agent.execution`: fail-closed executor with re-quote, re-check, DB-verified human approvals, idempotent buys, an `unknown` status for uncertain outcomes, and settlement tracking that resumes after restarts.
- Single-instance Postgres advisory lock; losing it halts trading.
- Migration 002 (execution safety). Deployment kit: `scripts/vm_setup.sh`, `scripts/deploy.sh`, nightly `scripts/backup.sh`, `docs/deploy.md`.
- `agent.risk`: fail-closed RiskGate with 11 deterministic rules, human-approval triggers and property-based tests; reviewed by the `risk-reviewer` subagent.
- `agent.decision`: SystemOne client (Jev, Clef), Claude Haiku fallback, strict answer validation, and a router with timeouts, circuit breaker and per-provider daily spend caps that degrades to HOLD.
- Repository bootstrap: `pyproject.toml` with runtime and dev dependencies, `.env.example` listing every secret by name, git ignore rules and LF line endings.
- `CLAUDE.md` with agent rules, layout, commands and commit conventions.
- `BREAKLOG.md` incident log (first entry: Deriv API platform change).
- `playbook.md` seeded with starting assumptions and reflection limits.
- `agent.config.Settings`: every trading limit and threshold in one validated place.
- `agent.deriv.guard`: demo-only WebSocket and account checks that fail closed.
- `agent.main`: runs WebSocket, tick streaming, batched persistence and heartbeat; honours `KILL_SWITCH`; graceful shutdown.
- `scripts/run_with_fake_server.py`: offline end-to-end smoke run with forced disconnects.
- Postgres schema (`001_init.sql`: ticks, decisions, risk verdicts, approvals, trades, shadow decisions, playbook revisions, events, state, eval runs) with a forward-only migration runner.
- `agent.db.repo`: batched `TickWriter`, `EventLog`, `StateStore`.
- `docker-compose.yml` (Postgres tuned for a 1 GB VM, agent service with restart policy and log rotation) and a non-root `Dockerfile`.
- `agent.deriv.ws`: WebSocket client with req_id routing, persistent subscriptions, watchdog and backoff reconnect using a fresh OTP each time.
- `agent.deriv.rest`: demo account selection and OTP WebSocket URL retrieval.
- `tests/fakes/fake_deriv_server.py`: offline stand-in for Deriv's demo WebSocket.
- `agent.market`: tick ring buffer with stale-data guard, and explainable indicators (returns, volatility, streak, z-score).
- `agent.clock`: injectable clock (`SystemClock`, `FakeClock`).
- GitHub Actions CI: ruff, mypy and pytest with a Postgres service on every push and PR.
- Claude Code workflow: post-edit hook (ruff + unit tests) and a `risk-reviewer` subagent for safety-critical paths.
- P0 spike scripts: `scripts/spike_deriv.py` (accounts → OTP → ticks → proposal → demo buy, refuses non-`/ws/demo` URLs), `scripts/spike_decision_models.py` (Jev, Clef-flash, Claude Haiku on the same snapshot), `scripts/spike_telegram.py` (chat id + approval buttons).
- `PLAN.md`: phased build plan (P0–P7) with tasks, named tests, exit criteria, UI plan, and a testing and benchmarking strategy (fault injection + shadow baselines).
