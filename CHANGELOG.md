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
- `agent.clock`: injectable clock (`SystemClock`, `FakeClock`).
- Claude Code workflow: post-edit hook (ruff + unit tests) and a `risk-reviewer` subagent for safety-critical paths.
- P0 spike scripts: `scripts/spike_deriv.py` (accounts → OTP → ticks → proposal → demo buy, refuses non-`/ws/demo` URLs), `scripts/spike_decision_models.py` (Jev, Clef-flash, Claude Haiku on the same snapshot), `scripts/spike_telegram.py` (chat id + approval buttons).
- `PLAN.md`: phased build plan (P0–P7) with tasks, named tests, exit criteria, UI plan, and a testing and benchmarking strategy (fault injection + shadow baselines).
