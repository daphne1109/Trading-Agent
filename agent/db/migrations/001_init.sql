-- 001_init: core schema. All timestamps are UTC (timestamptz).

CREATE TABLE ticks (
    id           bigserial PRIMARY KEY,
    symbol       text        NOT NULL,
    epoch        bigint      NOT NULL,
    quote        double precision NOT NULL,
    received_at  timestamptz NOT NULL,
    UNIQUE (symbol, epoch)
);
CREATE INDEX ticks_received_at_idx ON ticks (received_at);

-- One row per decision round, including HOLDs, skips and model failures.
CREATE TABLE decisions (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    symbol         text        NOT NULL,
    provider       text,
    model          text,
    prompt_hash    text,
    playbook_rev   integer,
    snapshot       jsonb,
    action         text CHECK (action IN ('CALL', 'PUT', 'HOLD')),
    probabilities  jsonb,
    confidence     double precision,
    latency_ms     integer,
    input_tokens   integer,
    output_tokens  integer,
    cost_usd       numeric(12, 6),
    skipped_reason text,
    error          text
);
CREATE INDEX decisions_created_at_idx ON decisions (created_at);

CREATE TABLE risk_verdicts (
    decision_id  bigint PRIMARY KEY REFERENCES decisions (id),
    checked_at   timestamptz NOT NULL DEFAULT now(),
    verdict      text NOT NULL CHECK (verdict IN ('APPROVE', 'REJECT', 'NEEDS_APPROVAL', 'NO_TRADE')),
    reasons      text[] NOT NULL DEFAULT '{}',
    rule_results jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE approvals (
    id             bigserial PRIMARY KEY,
    decision_id    bigint NOT NULL UNIQUE REFERENCES decisions (id),
    status         text   NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    trigger        text   NOT NULL,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    responded_at   timestamptz,
    tg_message_id  bigint
);

-- decision_id is UNIQUE: one decision can never buy twice (idempotency).
CREATE TABLE trades (
    id             bigserial PRIMARY KEY,
    decision_id    bigint NOT NULL UNIQUE REFERENCES decisions (id),
    contract_id    text,
    contract_type  text   NOT NULL,
    symbol         text   NOT NULL,
    stake          numeric(12, 2) NOT NULL,
    duration_ticks integer NOT NULL,
    buy_price      numeric(12, 2),
    payout         numeric(12, 2),
    status         text   NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'open', 'won', 'lost', 'sold', 'error')),
    profit         numeric(12, 2),
    bought_at      timestamptz,
    settled_at     timestamptz,
    error          text,
    raw            jsonb
);
CREATE INDEX trades_bought_at_idx ON trades (bought_at);

-- Paper decisions of simple baseline strategies on the same snapshot (PLAN.md 5b).
CREATE TABLE shadow_decisions (
    id            bigserial PRIMARY KEY,
    decision_id   bigint NOT NULL REFERENCES decisions (id),
    strategy      text   NOT NULL,
    action        text   NOT NULL CHECK (action IN ('CALL', 'PUT', 'HOLD')),
    p_call        double precision,
    settled_up    boolean,
    paper_profit  numeric(12, 4),
    UNIQUE (decision_id, strategy)
);

CREATE TABLE playbook_revisions (
    id          serial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    content     text   NOT NULL,
    diff        text,
    model       text,
    cost_usd    numeric(12, 6),
    stats       jsonb
);

CREATE TABLE agent_events (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    level    text NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
    kind     text NOT NULL,
    message  text NOT NULL,
    data     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX agent_events_ts_idx ON agent_events (ts);

CREATE TABLE agent_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eval_runs (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    git_sha     text,
    prompt_hash text,
    provider    text,
    metrics     jsonb NOT NULL,
    passed      boolean NOT NULL
);
