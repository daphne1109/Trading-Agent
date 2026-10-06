# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- Claude Code workflow: post-edit hook (ruff + unit tests) and a `risk-reviewer` subagent for safety-critical paths.
- P0 spike scripts: `scripts/spike_deriv.py` (accounts → OTP → ticks → proposal → demo buy, refuses non-`/ws/demo` URLs), `scripts/spike_decision_models.py` (Jev, Clef-flash, Claude Haiku on the same snapshot), `scripts/spike_telegram.py` (chat id + approval buttons).
- `PLAN.md`: phased build plan (P0–P7) with tasks, named tests, exit criteria, UI plan, and a testing and benchmarking strategy (fault injection + shadow baselines).
