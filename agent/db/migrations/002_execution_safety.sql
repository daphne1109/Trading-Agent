-- 002: execution safety (from the risk review of the executor).

-- A buy whose outcome is uncertain (timeout / dropped connection after sending) is `unknown`:
-- counted as open and as a worst-case loss until reconciled, never silently as `error`.
ALTER TABLE trades DROP CONSTRAINT trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check
    CHECK (status IN ('pending', 'open', 'unknown', 'won', 'lost', 'sold', 'error'));
ALTER TABLE trades ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();

-- The executor reads the decision from the DB instead of trusting its caller.
ALTER TABLE decisions ADD COLUMN provider_is_primary boolean;

-- Approvals expire, and record exactly which triggers the human saw.
-- Default is the epoch, i.e. already expired: an approval without an explicit expiry never counts.
ALTER TABLE approvals ADD COLUMN expires_at timestamptz NOT NULL DEFAULT 'epoch';
ALTER TABLE approvals ADD COLUMN reasons text[] NOT NULL DEFAULT '{}';
