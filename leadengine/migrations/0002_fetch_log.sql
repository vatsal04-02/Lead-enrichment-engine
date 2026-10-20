-- Audit trail of every request the engine makes (pages, robots.txt, browser sub-requests, cache hits, refusals).
CREATE TABLE IF NOT EXISTS fetch_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    job_id      UUID,
    kind        TEXT NOT NULL,                 -- page | robots | subresource
    host        TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      INT,
    from_cache  BOOLEAN NOT NULL DEFAULT FALSE,
    bytes       BIGINT NOT NULL DEFAULT 0,
    elapsed_ms  INT,
    retries     INT NOT NULL DEFAULT 0,
    error_kind  TEXT,                          -- blocked | robots | bot_challenge | timeout | http | ...
    error       TEXT
);
CREATE INDEX IF NOT EXISTS fetch_log_ts_idx ON fetch_log (ts);
CREATE INDEX IF NOT EXISTS fetch_log_job_idx ON fetch_log (job_id) WHERE job_id IS NOT NULL;
