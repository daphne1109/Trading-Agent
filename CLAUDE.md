# CLAUDE.md

Always-on AI trading agent on a **Deriv demo account**. The point is safe, observable, self-improving agent engineering, not profit. The full build plan is in `PLAN.md`.

## Rules that never change
1. **Demo only.** Code connects only to `wss://api.derivws.com/trading/v1/options/ws/demo`. `agent/deriv/guard.py` refuses anything else. Never weaken, bypass or mock away the guard outside tests.
2. **The model proposes, code decides.** The decision model picks only `CALL` / `PUT` / `HOLD` plus probabilities. Stake, duration, limits and execution are deterministic code in `agent/risk/` and `agent/execution/`. No code path may reach `buy` without passing `RiskGate`, and the executor re-runs the gate after re-quoting.
3. **Secrets live only in `.env`.** Never print, log or commit them. `.env.example` has names only.
4. **The nightly reflection may only append lessons to the playbook.** It can never change risk parameters.
5. **Honest claims.** No profitability claims anywhere. Volatility indices are random by design.
6. **Log incidents** in `BREAKLOG.md` when they happen.

## Layout
- `agent/` is the runtime: `deriv/` (REST, WS, guard), `market/` (feed, indicators), `decision/`, `risk/`, `execution/`, `hitl/`, `reflection/`, `reporting/`, `db/`.
- `web/` is the read-only dashboard. `eval/` is the replay eval. `scripts/` holds the spikes and ops.
- `tests/unit` (no I/O), `tests/integration` (fake Deriv server / Postgres), `tests/fakes`.

## Commands
```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use .venv/bin/activate on Linux
pip install -e ".[dev]"
pytest -q                      # all tests
pytest -q tests/unit           # fast tests only
ruff check . && ruff format --check .
mypy agent
docker compose up -d --build   # postgres + agent
```

## Conventions
- Python 3.11+, asyncio, type hints everywhere. Pydantic for anything parsed from outside.
- Time comes from `agent.clock.Clock`, never `time.time()` directly in logic, so tests can control it.
- Risk rules are pure functions `(ctx) -> RuleResult` with no I/O.
- **Any change under `agent/risk/` or `agent/deriv/guard.py` needs a test and a review by the `risk-reviewer` subagent.**
- All times in the DB are UTC. Display times in Asia/Kuala_Lumpur.

## Git
- Conventional Commits: `feat(scope): ...`, `fix(scope): ...`, `test`, `docs`, `chore`, `ci`, `refactor`. Imperative mood, subject ≤ 72 chars, a body explaining *why* when it isn't obvious.
- Small focused commits; push after each one.
- Add a line to `CHANGELOG.md` under `[Unreleased]` in the same commit as any user-visible change.
