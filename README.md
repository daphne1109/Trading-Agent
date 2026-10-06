# Trading Agent

An always-on AI agent that trades on a **Deriv demo account** (virtual money only). It streams live prices, asks a decision model for a direction, checks every answer against hard-coded risk rules, asks a human on Telegram before risky trades, and reviews its own results every night.

> **The goal is safe, observable, self-improving agent engineering, not profit.** The agent trades Deriv's synthetic volatility indices, which are random by design; no model can predict them, and this project makes no profitability claims.

**Status:** in active development. See [`PLAN.md`](PLAN.md) for the phased plan and [`CHANGELOG.md`](CHANGELOG.md) for progress.

## How it works

```
Deriv WS (demo) ─ticks─▶ MarketFeed ─snapshot─▶ Decision model (Jev) ─CALL/PUT/HOLD─▶ RiskGate (code)
     ▲                   stale guard                     ▲                            │
     │                                              playbook.md            reject / ask human / approve
     └──────────── Executor ◀────────── approved ◀── Telegram ◀──────────────────────┘
                      │
                      ▼
                  Postgres decision log ──▶ nightly reflection ──▶ playbook.md
```

**The model proposes, code decides.** The model only picks a direction. Stake, limits, the kill switch and execution are deterministic code that the model can't override.

## Safety model
- **Demo only:** the agent can only connect to Deriv's `/ws/demo` endpoint, and refuses anything else before connecting (`agent/deriv/guard.py`).
- **Fixed stake** set in config, never chosen by the model.
- **Risk gate:** a confidence floor, max open positions, a daily loss cap, a cooldown after losses and a stale-data check.
- **Human in the loop:** grey-zone trades need approval on Telegram; `/pause` and `/kill` work from a phone.
- **Spend caps** per AI provider per day.

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker compose up -d postgres
pytest -q                                          # unit + integration tests
python scripts/run_with_fake_server.py --seconds 30 # full agent against a local fake Deriv server
```

To run against Deriv, copy `.env.example` to `.env`, add a demo-account token, then `docker compose up -d --build`.

## Built with Claude Code
This repo is built AI-first. [`CLAUDE.md`](CLAUDE.md) holds the rules every AI-assisted change must follow. A post-edit hook runs lint and the unit tests after each edit, and a [`risk-reviewer`](.claude/agents/risk-reviewer.md) subagent reviews every change to the safety code. Incidents and what they taught are logged in [`BREAKLOG.md`](BREAKLOG.md).
