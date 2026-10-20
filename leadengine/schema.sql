-- Idempotent schema. Applied by `leadengine init-db` and automatically on startup.

CREATE TABLE IF NOT EXISTS companies (
    id              BIGSERIAL PRIMARY KEY,
    domain          TEXT NOT NULL UNIQUE,          -- normalised identity key (registrable domain)
    website_url     TEXT NOT NULL,
    input_name      TEXT,
    input_location  TEXT,
    input_industry  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_enriched_at TIMESTAMPTZ,
    last_outcome    TEXT
);

-- One row per (company, field). status='not_found' rows record "we looked and found nothing"
-- so repeated runs do not redo extraction until refresh_after.
CREATE TABLE IF NOT EXISTS company_fields (
    company_id          BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field               TEXT NOT NULL,
    value               TEXT,
    status              TEXT NOT NULL CHECK (status IN ('found','not_found')),
    source_url          TEXT,
    method              TEXT,                      -- json_ld | meta | mailto | tel | microdata | text_regex | link | browser_* | ai_grounded | input
    tier                TEXT,                      -- tier2_http | tier3_targeted | tier4_browser | tier5_ai | input
    method_prior        REAL,                      -- static heuristic prior for the method; NOT a correctness probability
    extracted_at        TIMESTAMPTZ NOT NULL,
    refresh_after       TIMESTAMPTZ NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified','failed')),
    verification_method TEXT,
    checks              JSONB NOT NULL DEFAULT '{}',
    source_hash         TEXT,                      -- content hash of the page the value came from
    alternates          JSONB NOT NULL DEFAULT '[]',
    note                TEXT,
    PRIMARY KEY (company_id, field)
);

CREATE TABLE IF NOT EXISTS company_field_history (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    old_source_url  TEXT,
    new_source_url  TEXT,
    method          TEXT,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_field_history_idx ON company_field_history (company_id, field, changed_at DESC);

-- HTTP response cache (honours Cache-Control / ETag / Last-Modified; never stores no-store/private).
CREATE TABLE IF NOT EXISTS http_cache (
    url             TEXT PRIMARY KEY,
    final_url       TEXT NOT NULL,
    status          INT NOT NULL,
    content_type    TEXT,
    etag            TEXT,
    last_modified   TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    body            BYTEA,
    body_hash       TEXT
);
CREATE INDEX IF NOT EXISTS http_cache_expires_idx ON http_cache (expires_at);

-- Parsed-candidates cache keyed by page content hash: unchanged pages are not re-parsed.
CREATE TABLE IF NOT EXISTS extraction_cache (
    url                 TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    result              JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (url, content_hash, extractor_version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type            TEXT NOT NULL,                 -- enrich_companies | enrich_csv
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','partially_completed','failed','cancelled')),
    input_ref       TEXT,                          -- original filename / description
    requested_fields TEXT[] NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',   -- budgets, csv mapping, options
    total           INT NOT NULL DEFAULT 0,
    processed       INT NOT NULL DEFAULT 0,
    succeeded       INT NOT NULL DEFAULT 0,
    failed          INT NOT NULL DEFAULT 0,
    attempts        INT NOT NULL DEFAULT 0,        -- times a worker (re)claimed this job
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    claimed_by      TEXT,
    lease_expires_at TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    error_summary   JSONB NOT NULL DEFAULT '{}',
    stats           JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS job_items (
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_index       INT NOT NULL,                  -- stable id: 1-based data-row number in the input
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','succeeded','failed','invalid','duplicate','skipped')),
    input           JSONB NOT NULL,                -- original columns, untouched
    domain          TEXT,
    company_id      BIGINT REFERENCES companies(id),
    duplicate_of    INT,
    attempts        INT NOT NULL DEFAULT 0,
    not_before      TIMESTAMPTZ,
    claimed_by      TEXT,
    lease_expires_at TIMESTAMPTZ,
    outcome         TEXT,                          -- enriched | cached | partial | no_data
    result          JSONB,
    error_kind      TEXT,
    error           TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    PRIMARY KEY (job_id, row_index)
);
CREATE INDEX IF NOT EXISTS job_items_claim_idx ON job_items (job_id, status, not_before);
CREATE INDEX IF NOT EXISTS job_items_domain_idx ON job_items (job_id, domain);

-- One row per company enrichment run: per-tier measurements for the metrics endpoint/benchmark.
CREATE TABLE IF NOT EXISTS enrichment_runs (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    job_id          UUID,
    row_index       INT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms     INT NOT NULL,
    outcome         TEXT NOT NULL,
    requested       TEXT[] NOT NULL,
    fields_found    INT NOT NULL DEFAULT 0,
    http_requests   INT NOT NULL DEFAULT 0,
    http_cache_hits INT NOT NULL DEFAULT 0,
    bytes_downloaded BIGINT NOT NULL DEFAULT 0,
    ai_calls        INT NOT NULL DEFAULT 0,
    browser_ops     INT NOT NULL DEFAULT 0,
    retries         INT NOT NULL DEFAULT 0,
    cost_usd_est    DOUBLE PRECISION NOT NULL DEFAULT 0,
    tiers           JSONB NOT NULL DEFAULT '{}',
    cost            JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS enrichment_runs_job_idx ON enrichment_runs (job_id);
