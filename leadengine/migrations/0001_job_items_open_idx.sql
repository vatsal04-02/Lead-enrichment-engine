-- The claim query scans the PK in row order filtering by status; with this partial index its cost depends on the
-- number of OPEN items instead of the size of the job (measured 35-57 ms at 200k rows with 99.5% done before).
CREATE INDEX IF NOT EXISTS job_items_open_idx ON job_items (job_id, row_index) WHERE status IN ('queued', 'running');
