-- Core schema. Every table that holds tenant data has a tenant_id column and is
-- protected by row-level security in 002_rls.sql.

CREATE EXTENSION IF NOT EXISTS vector;

-- Directory of known tenants. Not tenant data: names are needed to reject
-- unknown tenants and to stop one tenant's name leaking into another's draft.
CREATE TABLE tenants (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    aliases     text[] NOT NULL DEFAULT '{}'
);

INSERT INTO tenants (id, name, aliases) VALUES
    ('northwind', 'Northwind Cycles', ARRAY['Northwind']),
    ('harbor',    'Harbor Dental',    ARRAY['Harbor']);

CREATE TABLE kb_articles (
    tenant_id   text NOT NULL REFERENCES tenants(id),
    id          text NOT NULL,
    title       text NOT NULL,
    body        text NOT NULL,
    internal    boolean NOT NULL DEFAULT false,
    embedding   vector(768) NOT NULL,
    embed_model text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

-- Hashed 5-word shingles of every KB body. The app role cannot read this table;
-- it only gets other tenants' hashes through relay_foreign_fingerprints(), so the
-- checker can detect copied content without ever seeing another tenant's text.
CREATE TABLE kb_fingerprints (
    tenant_id   text NOT NULL REFERENCES tenants(id),
    kb_id       text NOT NULL,
    shingle_md5 text NOT NULL,
    PRIMARY KEY (tenant_id, kb_id, shingle_md5)
);

-- One row per run (demo, run, eval). Holds only counters, no tenant data.
CREATE TABLE runs (
    run_id      text PRIMARY KEY,
    kind        text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    tokens_used bigint NOT NULL DEFAULT 0,
    token_budget bigint NOT NULL
);

CREATE TABLE tickets (
    tenant_id       text NOT NULL REFERENCES tenants(id),
    ticket_id       text NOT NULL,
    customer_ref    text NOT NULL,
    subject         text NOT NULL,
    body            text NOT NULL,
    received_at     timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'received',
    rejection_count int NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, ticket_id)
);

-- The lead agent's final decision. One row per ticket, ever: re-running an
-- inbox cannot create a second decision.
CREATE TABLE decisions (
    tenant_id          text NOT NULL,
    ticket_id          text NOT NULL,
    run_id             text NOT NULL REFERENCES runs(run_id),
    category           text NOT NULL,
    priority           text NOT NULL,
    action             text NOT NULL CHECK (action IN ('draft_reply', 'ask_for_info', 'escalate')),
    injection_detected boolean NOT NULL,
    kb_refs            text[] NOT NULL DEFAULT '{}',
    draft_reply        text,
    status             text NOT NULL CHECK (status IN ('pending_approval', 'escalated', 'held_budget', 'failed')),
    model              text NOT NULL,
    tokens_in          int NOT NULL,
    tokens_out         int NOT NULL,
    latency_ms         int NOT NULL,
    failure_class      text,
    trace              jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ticket_id),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id)
);

-- Every customer-facing text that needs a human. A redraft after rejection is a
-- new version; only one version per ticket can be pending at a time.
CREATE TABLE approvals (
    tenant_id   text NOT NULL,
    ticket_id   text NOT NULL,
    version     int NOT NULL,
    action      text NOT NULL,
    draft       text NOT NULL,
    kb_refs     text[] NOT NULL DEFAULT '{}',
    state       text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'rejected')),
    decided_by  text,
    decided_at  timestamptz,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ticket_id, version),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id)
);
CREATE UNIQUE INDEX approvals_one_pending ON approvals (tenant_id, ticket_id) WHERE state = 'pending';

-- Approved messages. UNIQUE (tenant_id, ticket_id) makes "at most once" a
-- database guarantee, not a convention.
CREATE TABLE outbox (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    ticket_id       text NOT NULL,
    text            text NOT NULL,
    approved_by     text NOT NULL,
    approved_at     timestamptz NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    UNIQUE (tenant_id, ticket_id),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id)
);

CREATE TABLE cases (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    ticket_id   text NOT NULL,
    source      text NOT NULL,
    owner       text NOT NULL,
    severity    text NOT NULL CHECK (severity IN ('sev1', 'sev2', 'sev3', 'sev4')),
    reason      text NOT NULL,
    status      text NOT NULL DEFAULT 'open',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, ticket_id, source)
);

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    ticket_id   text,
    actor       text NOT NULL,
    action      text NOT NULL,
    from_state  text,
    to_state    text,
    reason      text,
    details     jsonb NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_ticket ON audit_log (tenant_id, ticket_id, id);

CREATE TABLE model_calls (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    run_id      text,
    ticket_id   text,
    task        text NOT NULL,
    model       text NOT NULL,
    provider    text NOT NULL,
    tokens_in   int NOT NULL DEFAULT 0,
    tokens_out  int NOT NULL DEFAULT 0,
    latency_ms  int NOT NULL DEFAULT 0,
    ok          boolean NOT NULL,
    error_class text,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_calls_run ON model_calls (run_id);

-- Durable work queue. Jobs are claimed only through relay_claim_job().
CREATE TABLE jobs (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    ticket_id       text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('process', 'redraft')),
    payload         jsonb NOT NULL DEFAULT '{}',
    idempotency_key text NOT NULL UNIQUE,
    run_id          text NOT NULL,
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'leased', 'done', 'dead')),
    attempts        int NOT NULL DEFAULT 0,
    max_attempts    int NOT NULL,
    run_after       timestamptz NOT NULL DEFAULT now(),
    lease_until     timestamptz,
    leased_by       text,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready ON jobs (status, run_after);

-- Input lines that were never processed: unknown tenant, bad JSON, missing
-- fields. Deliberately outside any tenant: by definition they have none.
CREATE TABLE rejected_inputs (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    line_no     int NOT NULL,
    claimed_tenant text,
    ticket_id   text,
    reason      text NOT NULL,
    raw_sha256  text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, raw_sha256)
);
