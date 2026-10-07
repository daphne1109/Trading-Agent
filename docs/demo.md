# Demo script: showing Sentinel in person

## Before you leave (5 min)
1. Start Docker Desktop, then in the project folder: `docker compose up -d --build`.
2. Open http://localhost:8080 and **leave it running**. The agent needs ~2 minutes of price history, and the "model vs strategies" table fills up the longer it runs.
3. Check the status light says **LIVE**. If it says OFFLINE, see *If something goes wrong* below.
4. Optional but better: put an AI key in `.env` (`ANTHROPIC_API_KEY` or `TYPESAFE_API_KEY`) and run the same command, so the status bar shows a real model instead of the rule-based stand-in.
5. Keep GitHub open in a second tab: https://github.com/daphne1109/Trading-Agent

At the venue the laptop needs internet (the feed is live from Deriv). Phone hotspot works fine.

## The 60-second pitch
> "Sentinel is an autonomous trading agent I'm building, where the AI makes the call but can never place a trade on its own authority.
> It streams live prices from Deriv's API every second. Every 30 seconds an AI model says up, down or wait, with probabilities.
> That answer then goes through a risk gate: eleven rules in plain code that the AI can't skip, like a confidence floor, a daily loss cap and a cooldown after losses. Risky calls are escalated to a human.
> If it passes, the executor gets a fresh real price quote from Deriv, checks everything again, and fills the trade.
> Every step is logged, and you can click any decision to see exactly why it was allowed or blocked.
> And honestly: the market is random by design, so the point isn't profit. It's showing how to run an AI agent safely and observably."

## What to click (2 to 3 minutes)
1. **Status bar:** "LIVE, last tick 1 s ago, real Deriv prices." Point out **PAPER MODE** (see the Q&A).
2. **Pipeline strip:** walk through the five steps; stress step 3, "the AI can't skip this."
3. **Chart:** the line is the live index. Triangles are trades (blue = up/CALL, orange = down/PUT); red circles are trades the gate **blocked** or that needed a human. Click a triangle.
4. **Decision page** (the best screen to show):
   - **What the model saw:** the price sparkline and indicators.
   - **Probabilities.**
   - **Risk gate verdict:** every rule with ✓ / ✕ / ! and the reason.
   - **Execution:** the real Deriv quote ($1 stake → $1.95 payout), entry and exit tick, result.
   - **Same moment, other strategies:** what the coin flip and trend rule would have done.
5. **Back to Overview:**
   - **"Blocked by risk gate"** is the number to be proud of.
   - **"Model vs simple strategies":** "on a random market everyone sits near 50%. That's the honest baseline, and I measure the AI against it rather than claiming an edge."
6. **GitHub (if they're technical):**
   - `BREAKLOG.md`: real incidents. *"An AI code reviewer I set up found that NaN inputs could get a trade approved, before it ever shipped."*
   - The commit history and the 280+ tests.

## Likely questions
**Why paper trading?**
Deriv's trading API isn't available to Malaysian residents. I didn't work around that. Prices and quotes are real, from Deriv's public API; only the final order is simulated, settled on real ticks with Deriv's rules. A full demo-account executor is built and tested behind one setting.

**Does it make money?**
No, and it can't be expected to. Volatility indices are generated randomly, and Rise/Fall pays 1.95×, so break-even is ~51.3%. The dashboard compares the AI with a coin flip so nobody has to take my word for it.

**What's the AI actually doing?**
It reads recent prices and indicators plus a "playbook" of lessons, then returns up/down/wait with probabilities. The plan is TypeSafe's Jev (a fast decision model), with Claude as a fallback and for the nightly self-review.

**What stops it from going rogue?**
- The AI never chooses the amount or places orders.
- Every trade passes 11 coded rules, twice: once when decided and again just before execution.
- Anything uncertain fails closed. An order with an unknown outcome counts as a loss until resolved.
- There's a kill switch, and only one instance can run at a time.

**What's next?**
- Telegram approvals and kill switch from a phone.
- A nightly reflection where the agent writes lessons from its own results.
- A replay eval in CI that blocks a worse prompt from shipping.
- Deploying it 24/7 with a public dashboard link.

**How long did this take / how did you build it?**
A few days, AI-first with Claude Code: a post-edit test hook, plus a dedicated AI reviewer for the safety code. That reviewer caught real bugs, which are logged in the BREAKLOG.

## If something goes wrong
| Symptom | Fix |
|---|---|
| Page won't load | Docker Desktop isn't running, or the containers stopped: `docker compose up -d` |
| Status OFFLINE / no ticks | No internet, or the venue blocks WebSockets: switch to a phone hotspot |
| "Waiting for live ticks…" | Just started; wait 10 seconds |
| No decisions yet | The first ~2 minutes are warm-up (the feed shows "skipped: only N/120 ticks") |
| Everything says "needs human" | That's the safety design: mid-confidence calls need approval, and Telegram approvals aren't built yet. It's a good talking point. |

Stop everything afterwards with `docker compose down` (your data is kept).
