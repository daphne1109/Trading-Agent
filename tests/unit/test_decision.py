import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx

from agent.clock import FakeClock
from agent.decision.base import Action, Decision
from agent.decision.claude import ClaudeDecisionModel, _Answer
from agent.decision.prompt import load_prompt
from agent.decision.router import DecisionRouter
from agent.decision.systemone import SystemOneModel
from agent.decision.validate import InvalidModelOutput, validate_answer
from agent.spend import SpendTracker

URL = "https://api.typesafe.ai/v1/systemone"
QUESTION = load_prompt().question
STATE = {"symbol": "1HZ100V", "last_ticks": [1.0, 1.1]}


def jev_response(choice="CALL", probs=None, confidence=0.7, input_tokens=2000):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "action": {
                "type": "choice",
                "choice": choice,
                "probabilities": probs or {"CALL": 0.7, "PUT": 0.2, "HOLD": 0.1},
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }


def jev(http, name="jev"):
    return SystemOneModel(
        name=name,
        url=URL,
        api_key="k",
        model="jev-1.13.0",
        http=http,
        usd_per_m_input=Decimal("0.042"),
    )


# ------------------------------------------------------------------ prompt + validation


def test_prompt_loads_with_stable_hash():
    a, b = load_prompt(), load_prompt()
    assert a.hash == b.hash and len(a.hash) == 12
    assert set(a.question["criteria"]) == {"CALL", "PUT", "HOLD"}


def test_validate_uses_cautious_confidence():
    _, _, conf = validate_answer("CALL", {"CALL": 0.6, "PUT": 0.3, "HOLD": 0.1}, 0.9)
    assert conf == 0.6  # model said 0.9 but only priced CALL at 0.6


@pytest.mark.parametrize(
    ("choice", "probs", "conf"),
    [
        ("BUY", {"CALL": 1.0}, 0.9),  # unknown action
        ("CALL", {"CALL": 0.9, "PUT": 0.9}, 0.9),  # sums to 1.8
        ("CALL", {"CALL": float("nan"), "PUT": 0.5}, 0.9),
        ("CALL", {"CALL": 1.2, "PUT": -0.2}, 0.9),
        ("CALL", {"CALL": 0.5, "MOON": 0.5}, 0.9),  # unknown key
        ("PUT", {"CALL": 1.0}, 0.9),  # no probability for the chosen action
        ("CALL", {"CALL": 1.0}, "high"),
        ("CALL", {"CALL": 1.0}, True),
        ("CALL", {}, 0.9),
        ("CALL", None, 0.9),
    ],
)
def test_validate_rejects_malformed_answers(choice, probs, conf):
    with pytest.raises(InvalidModelOutput):
        validate_answer(choice, probs, conf)


# ------------------------------------------------------------------ SystemOne client


@respx.mock
async def test_systemone_parses_answer():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=jev_response()))
    async with httpx.AsyncClient() as http:
        d = await jev(http).decide(STATE, QUESTION)
    assert d.action is Action.CALL
    assert d.confidence == 0.7
    assert sum(d.probabilities.values()) == pytest.approx(1)
    assert d.cost_usd == Decimal("0.000084")  # 2000 tokens * $0.042 / 1M
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer k"
    assert b'"questions"' in sent.content


def test_systemone_unwraps_cloudflare_envelope():
    model = jev(None, name="clef")
    d = model.parse({"success": True, "result": jev_response(choice="PUT", probs={"PUT": 1.0})}, 5)
    assert d.action is Action.PUT and d.provider == "clef"


@pytest.mark.parametrize(
    "payload", [{}, {"answers": {}}, {"answers": {"action": "CALL"}}, [], "junk"]
)
def test_systemone_bad_payload_raises(payload):
    with pytest.raises(InvalidModelOutput):
        jev(None).parse(payload, 1)


@respx.mock
async def test_systemone_http_error_raises():
    respx.post(URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await jev(http).decide(STATE, QUESTION)


# ------------------------------------------------------------------ Claude fallback


class FakeAnthropic:
    def __init__(self, answer=None, stop_reason="end_turn"):
        async def parse(**kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                parsed_output=answer,
                stop_reason=stop_reason,
                usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
            )

        self.messages = SimpleNamespace(parse=parse)


async def test_claude_fallback_maps_to_decision():
    answer = _Answer(action="PUT", p_call=0.2, p_put=0.7, p_hold=0.1, confidence=0.8)
    fake = FakeAnthropic(answer)
    d = await ClaudeDecisionModel(fake).decide(STATE, QUESTION)
    assert d.action is Action.PUT and d.confidence == 0.7
    assert d.calibrated is False
    assert d.cost_usd == Decimal("0.0015")  # 1000*$1/M + 100*$5/M
    assert fake.kwargs["model"] == "claude-haiku-4-5"


async def test_claude_refusal_raises():
    with pytest.raises(InvalidModelOutput):
        await ClaudeDecisionModel(FakeAnthropic(None, "refusal")).decide(STATE, QUESTION)


# ------------------------------------------------------------------ router


class StubModel:
    def __init__(self, name, result=None, exc=None, delay=0.0, cost="0.001"):
        self.name, self.result, self.exc, self.delay, self.calls = name, result, exc, delay, 0
        self.cost = Decimal(cost)

    async def decide(self, state, question):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result or Decision(
            action=Action.CALL,
            probabilities={"CALL": 0.8, "PUT": 0.2},
            confidence=0.8,
            provider=self.name,
            model="m",
            cost_usd=self.cost,
        )


def router(providers, clock=None, caps=None, **kw):
    clock = clock or FakeClock()
    caps = caps or {p.name: Decimal("1") for p in providers}
    return DecisionRouter(providers, SpendTracker(clock, caps), clock, **kw)


async def test_router_uses_primary_when_healthy():
    jev_m, clef = StubModel("jev"), StubModel("clef")
    out = await router([jev_m, clef]).decide(STATE, QUESTION)
    assert out.is_primary and out.decision.provider == "jev"
    assert clef.calls == 0


async def test_router_falls_back_on_timeout():
    slow, clef = StubModel("jev", delay=1), StubModel("clef")
    out = await router([slow, clef], timeout_s=0.05).decide(STATE, QUESTION)
    assert out.decision.provider == "clef"
    assert not out.is_primary
    assert "jev: TimeoutError" in out.attempts[0]


async def test_router_falls_back_on_invalid_output():
    bad, clef = StubModel("jev", exc=InvalidModelOutput("junk")), StubModel("clef")
    out = await router([bad, clef]).decide(STATE, QUESTION)
    assert out.decision.provider == "clef"


async def test_router_all_fail_returns_hold():
    out = await router(
        [StubModel("jev", exc=RuntimeError("down")), StubModel("clef", exc=RuntimeError("down"))]
    ).decide(STATE, QUESTION)
    assert out.decision.action is Action.HOLD
    assert "jev: RuntimeError: down" in out.decision.error
    assert not out.is_primary


async def test_router_circuit_breaker_opens_and_recovers():
    clock = FakeClock()
    flaky, clef = StubModel("jev", exc=RuntimeError("x")), StubModel("clef")
    r = router([flaky, clef], clock=clock, breaker_threshold=3, breaker_cooldown_s=300)
    for _ in range(3):
        await r.decide(STATE, QUESTION)
    assert flaky.calls == 3
    out = await r.decide(STATE, QUESTION)
    assert flaky.calls == 3  # skipped while open
    assert "jev: circuit open" in out.attempts
    clock.advance(301)
    flaky.exc = None
    out = await r.decide(STATE, QUESTION)
    assert out.is_primary and flaky.calls == 4


async def test_spend_cap_blocks_call():
    clock = FakeClock()
    expensive, clef = StubModel("jev", cost="0.60"), StubModel("clef")
    r = router([expensive, clef], clock=clock, caps={"jev": Decimal("0.50"), "clef": Decimal("1")})
    first = await r.decide(STATE, QUESTION)
    assert first.decision.provider == "jev"  # 0 < 0.50, allowed; now 0.60 spent
    second = await r.decide(STATE, QUESTION)
    assert second.decision.provider == "clef"
    assert expensive.calls == 1
    clock.advance(24 * 3600)  # new UTC day resets the cap
    third = await r.decide(STATE, QUESTION)
    assert third.decision.provider == "jev"


async def test_provider_without_cap_is_refused():
    out = await router([StubModel("jev")], caps={"other": Decimal("1")}).decide(STATE, QUESTION)
    assert out.decision.action is Action.HOLD
