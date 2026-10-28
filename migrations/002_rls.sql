-- Tenant isolation. The application connects as relay_app (created by the
-- migration runner: LOGIN, NOSUPERUSER, NOBYPASSRLS). Every tenant table is
-- FORCE ROW LEVEL SECURITY with a policy bound to the per-transaction setting
-- app.tenant_id. current_setting(..., true) returns NULL when the setting is
-- missing, NULL = x is never true, so a session that forgot to set a tenant
-- sees zero rows and cannot write any (fail closed).

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['kb_articles', 'tickets', 'decisions', 'approvals', 'outbox',
                             'cases', 'audit_log', 'model_calls', 'jobs']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                 USING (tenant_id = current_setting(''app.tenant_id'', true))
                 WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END $$;

-- kb_fingerprints: RLS on with no policy at all, so relay_app can never read it.
ALTER TABLE kb_fingerprints ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_fingerprints FORCE ROW LEVEL SECURITY;

-- Shingling must match relay.text.shingles() in Python exactly.
CREATE FUNCTION relay_shingles(p_text text) RETURNS SETOF text
LANGUAGE sql IMMUTABLE AS $$
    WITH w AS (
        SELECT array_remove(regexp_split_to_array(
                   regexp_replace(lower(p_text), '[^a-z0-9]+', ' ', 'g'), ' '), '') AS a
    )
    SELECT md5(array_to_string(a[i:i + 4], ' '))
    FROM w, generate_series(1, greatest(cardinality(a) - 4, 0)) AS i
$$;

CREATE FUNCTION relay_kb_fingerprint() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    DELETE FROM kb_fingerprints WHERE tenant_id = NEW.tenant_id AND kb_id = NEW.id;
    INSERT INTO kb_fingerprints (tenant_id, kb_id, shingle_md5)
        SELECT NEW.tenant_id, NEW.id, s FROM relay_shingles(NEW.body) AS s
        ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;

CREATE TRIGGER kb_fingerprint AFTER INSERT OR UPDATE OF body ON kb_articles
    FOR EACH ROW EXECUTE FUNCTION relay_kb_fingerprint();

-- Hashes of every OTHER tenant's KB shingles, for the checker. Returns hashes
-- only, never text, and refuses to run without a tenant.
CREATE FUNCTION relay_foreign_fingerprints() RETURNS SETOF text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE me text := current_setting('app.tenant_id', true);
BEGIN
    IF me IS NULL OR me = '' THEN
        RAISE EXCEPTION 'app.tenant_id is not set';
    END IF;
    RETURN QUERY SELECT DISTINCT shingle_md5 FROM kb_fingerprints WHERE tenant_id <> me;
END $$;

-- Lease the next ready job across all tenants. Returns identifiers only; the
-- worker then opens a transaction bound to that job's tenant to do the work.
-- A job whose lease expired (worker died) is claimable again.
CREATE FUNCTION relay_claim_job(p_worker text, p_lease_s int)
RETURNS TABLE (job_id bigint, tenant_id text, ticket_id text, kind text, attempts int, max_attempts int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    RETURN QUERY
    WITH next AS (
        SELECT j.id FROM jobs j
        WHERE (j.status = 'queued' AND j.run_after <= now())
           OR (j.status = 'leased' AND j.lease_until < now())
        ORDER BY j.run_after, j.id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE jobs j
       SET status = 'leased',
           attempts = j.attempts + 1,
           leased_by = p_worker,
           lease_until = now() + make_interval(secs => p_lease_s),
           updated_at = now()
      FROM next
     WHERE j.id = next.id
    RETURNING j.id, j.tenant_id, j.ticket_id, j.kind, j.attempts, j.max_attempts;
END $$;

-- Queue depth by status, for the operator. Counts only.
CREATE FUNCTION relay_queue_stats()
RETURNS TABLE (status text, n bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT status, count(*) FROM jobs GROUP BY status
$$;

REVOKE ALL ON FUNCTION relay_foreign_fingerprints() FROM PUBLIC;
REVOKE ALL ON FUNCTION relay_claim_job(text, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION relay_queue_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_foreign_fingerprints() TO relay_app;
GRANT EXECUTE ON FUNCTION relay_claim_job(text, int) TO relay_app;
GRANT EXECUTE ON FUNCTION relay_queue_stats() TO relay_app;

GRANT USAGE ON SCHEMA public TO relay_app;
GRANT SELECT ON tenants TO relay_app;
GRANT SELECT, INSERT, UPDATE ON runs, rejected_inputs TO relay_app;
GRANT SELECT, INSERT, UPDATE ON kb_articles, tickets, decisions, approvals, outbox,
                                cases, audit_log, model_calls, jobs TO relay_app;
-- Deleting a decision is how a held_budget ticket is re-queued.
GRANT DELETE ON decisions TO relay_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO relay_app;
