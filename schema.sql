CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) PRIMARY KEY,
    service_type VARCHAR(100) NOT NULL,
    status VARCHAR(32) NOT NULL,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_service_type ON jobs (service_type);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS job_inputs (
    job_id VARCHAR(36) PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    payload_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS job_results (
    job_id VARCHAR(36) PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    gherkin_text TEXT NOT NULL,
    result_json JSONB
);

CREATE TABLE IF NOT EXISTS artifacts (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    artifact_type VARCHAR(100) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    mime_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_artifacts_job_id ON artifacts (job_id);

CREATE TABLE IF NOT EXISTS job_logs (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    level VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_job_logs_job_id ON job_logs (job_id);
