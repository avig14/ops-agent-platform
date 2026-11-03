-- Ops Agent Platform: initial schema.
-- Conventions: ids are text (readable, e.g. CASE-0001) or uuid; all timestamps timestamptz.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ───────────────────────── identity ─────────────────────────
CREATE TABLE users (
  id            text PRIMARY KEY,
  email         text UNIQUE NOT NULL,
  name          text NOT NULL,
  role          text NOT NULL CHECK (role IN ('requester','manager','regional_head','finance_director','admin','service')),
  region        text,
  password_hash text NOT NULL,
  active        boolean NOT NULL DEFAULT true
);

-- ───────────────────────── reference data (seed) ─────────────────────────
CREATE TABLE stores (
  id text PRIMARY KEY, name text NOT NULL, city text NOT NULL, region text NOT NULL, opens_on date
);
CREATE TABLE suppliers (
  id text PRIMARY KEY, name text NOT NULL, email text, lead_time_days int NOT NULL DEFAULT 7,
  reliability numeric(3,2) NOT NULL DEFAULT 0.9
);
CREATE TABLE items (
  id text PRIMARY KEY, name text NOT NULL, unit_cost numeric(12,2) NOT NULL, supplier_id text REFERENCES suppliers(id)
);
CREATE TABLE inventory_snapshots (
  id bigserial PRIMARY KEY, item_id text NOT NULL REFERENCES items(id), location text NOT NULL,
  on_hand int NOT NULL, snapshot_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchase_orders (
  id text PRIMARY KEY, item_id text NOT NULL REFERENCES items(id), supplier_id text NOT NULL REFERENCES suppliers(id),
  qty int NOT NULL, destination_store_id text REFERENCES stores(id), promised_date date, status text NOT NULL DEFAULT 'open'
);

-- ───────────────────────── intake ─────────────────────────
CREATE TABLE source_messages (            -- raw inbound content; restricted. Audit log stores only id + hash.
  id            text PRIMARY KEY,
  channel       text NOT NULL CHECK (channel IN ('email','manual','supplier_update','system')),
  sender        text,
  message_id    text,                      -- RFC822 Message-ID or client idempotency key
  subject       text,
  body          text NOT NULL,
  sha256        text NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, message_id)
);

CREATE TABLE cases (
  id            text PRIMARY KEY,
  status        text NOT NULL,
  version       int  NOT NULL DEFAULT 1,
  title         text,
  intent        text,
  priority      text,
  risk_level    text,
  requester_id  text REFERENCES users(id),
  owner_id      text REFERENCES users(id),
  parent_id     text REFERENCES cases(id),
  duplicate_of  text REFERENCES cases(id),
  degraded      boolean NOT NULL DEFAULT false,
  needs_human   boolean NOT NULL DEFAULT false,
  injection_flag boolean NOT NULL DEFAULT false,
  deadline_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz
);
CREATE INDEX cases_status_idx ON cases(status);

CREATE TABLE case_sources (               -- many messages can belong to one case (revisions, replies, duplicates)
  case_id text NOT NULL REFERENCES cases(id), message_id text NOT NULL REFERENCES source_messages(id),
  role text NOT NULL CHECK (role IN ('origin','reply','revision','duplicate','cancel')),
  PRIMARY KEY (case_id, message_id)
);

CREATE TABLE case_versions (              -- facts snapshot per revision ("changing circumstances")
  case_id text NOT NULL REFERENCES cases(id), version int NOT NULL,
  facts jsonb NOT NULL, diff jsonb, reason text, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, version)
);

CREATE TABLE extracted_fields (
  id bigserial PRIMARY KEY, case_id text NOT NULL REFERENCES cases(id), version int NOT NULL,
  name text NOT NULL, value jsonb, quote text, confidence numeric(4,3), signals jsonb,
  state text NOT NULL DEFAULT 'ai' CHECK (state IN ('ai','confirmed','corrected','dropped')),
  corrected_by text REFERENCES users(id), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, version, name)
);

-- ───────────────────────── decisions ─────────────────────────
CREATE TABLE recommendations (
  id bigserial PRIMARY KEY, case_id text NOT NULL REFERENCES cases(id), version int NOT NULL, round int NOT NULL DEFAULT 1,
  options jsonb NOT NULL, chosen_option text, total_cost numeric(14,2) NOT NULL DEFAULT 0,
  rationale text, evidence jsonb, inputs_used int, inputs_total int, sufficiency text,
  status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected','superseded','abstained')),
  superseded_by bigint REFERENCES recommendations(id), created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
  id bigserial PRIMARY KEY, case_id text NOT NULL REFERENCES cases(id), recommendation_id bigint REFERENCES recommendations(id),
  round int NOT NULL DEFAULT 1, tier int NOT NULL DEFAULT 1, required_role text NOT NULL,
  approver_id text REFERENCES users(id), amount numeric(14,2) NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','escalated','withdrawn','expired')),
  reason text, deadline_at timestamptz, reminded_50 boolean NOT NULL DEFAULT false, reminded_90 boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz
);
CREATE INDEX approvals_pending_idx ON approvals(status, deadline_at);

CREATE TABLE policy_decisions (           -- why human approval was (or was not) required
  id bigserial PRIMARY KEY, case_id text NOT NULL REFERENCES cases(id), version int NOT NULL,
  requires_human boolean NOT NULL, rules_fired jsonb NOT NULL, inputs jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── comms ─────────────────────────
CREATE TABLE outbox (
  id bigserial PRIMARY KEY, case_id text REFERENCES cases(id), kind text NOT NULL, recipient text NOT NULL,
  channel text NOT NULL DEFAULT 'sandbox', subject text, body text NOT NULL,
  message_id text UNIQUE NOT NULL,                       -- idempotency key
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('draft','pending','sending','sent','failed','delivery_unknown','dead','cancelled')),
  attempts int NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz,
  last_error text, created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz
);
CREATE INDEX outbox_due_idx ON outbox(status, next_attempt_at);

-- ───────────────────────── agent + external-call traces ─────────────────────────
CREATE TABLE agent_messages (             -- A2A-style: who produced / consumed what
  id bigserial PRIMARY KEY, case_id text REFERENCES cases(id), task_id text, from_agent text NOT NULL, to_agent text,
  kind text NOT NULL CHECK (kind IN ('artifact','question','veto','event')), schema_version int NOT NULL DEFAULT 1,
  payload jsonb NOT NULL, trace_id text, span_id text, parent_span_id text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_messages_case_idx ON agent_messages(case_id, id);

CREATE TABLE external_calls (
  id bigserial PRIMARY KEY, case_id text, trace_id text, span_id text, parent_span_id text,
  kind text NOT NULL CHECK (kind IN ('llm','data','smtp')), target text NOT NULL, operation text,
  attempt int NOT NULL DEFAULT 1, status text NOT NULL, error_class text, http_status int, request_id text,
  latency_ms int, tokens_in int, tokens_out int, cache_hit boolean NOT NULL DEFAULT false,
  failover_from text, params_hash text, stale_age_s int, detail jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX external_calls_case_idx ON external_calls(case_id, id);

CREATE TABLE llm_cache (
  prompt_hash text PRIMARY KEY, model text NOT NULL, response jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── evaluation + feedback ─────────────────────────
CREATE TABLE eval_runs (
  id bigserial PRIMARY KEY, mode text NOT NULL CHECK (mode IN ('replay','live')), dataset_hash text, prompt_hash text,
  model text, git_sha text, summary jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE eval_results (
  id bigserial PRIMARY KEY, run_id bigint NOT NULL REFERENCES eval_runs(id), case_key text NOT NULL, metric text NOT NULL,
  value numeric, passed boolean, detail jsonb
);
CREATE TABLE feedback (
  id bigserial PRIMARY KEY, case_id text REFERENCES cases(id), user_id text REFERENCES users(id), target text NOT NULL,
  rating int NOT NULL, reason text, promote_to_golden boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── audit log (tamper-evident hash chain) ─────────────────────────
CREATE TABLE audit_log (
  id            bigserial PRIMARY KEY,
  chain_key     text NOT NULL,            -- case id, or 'SYSTEM'
  seq           bigint NOT NULL,          -- per chain, assigned by trigger, contiguous
  ts            timestamptz NOT NULL,     -- real DB clock, set by trigger (never simulated)
  sim_ts        timestamptz,              -- simulated clock when clock-offset demo is on
  clock_offset_active boolean NOT NULL DEFAULT false,
  actor_type    text NOT NULL CHECK (actor_type IN ('human','agent','system')),
  actor_id      text NOT NULL,
  action        text NOT NULL,
  outcome       text NOT NULL CHECK (outcome IN ('attempt','success','failure','blocked','degraded','skipped')),
  severity      text NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','error','critical')),
  reason_code   text,
  summary       text NOT NULL,
  diff          jsonb,                    -- before/after for overrides and revisions
  refs          jsonb,                    -- ids + hashes of inputs/outputs (no raw email or prompts)
  error         jsonb,                    -- {class, provider, http_status, request_id, message(scrubbed)}
  not_fetched   jsonb,                    -- [{source, operation, params_hash, reason_code, attempts, fallback_used, stale_age, affects}]
  model         text,
  prompt_version text,
  trace_id      text, span_id text, parent_span_id text,
  prev_hash     text NOT NULL,
  row_hash      text NOT NULL,
  UNIQUE (chain_key, seq)
);
CREATE INDEX audit_case_idx ON audit_log(chain_key, seq);
CREATE INDEX audit_outcome_idx ON audit_log(outcome, id);

-- Canonical text of a row. All timestamps in fixed UTC format; jsonb::text is deterministic.
-- Hashing happens ONLY in SQL (never in Python) so the two can never disagree.
CREATE FUNCTION audit_canonical(r audit_log) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT concat_ws('|',
    r.chain_key, r.seq::text,
    to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    coalesce(to_char(r.sim_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), ''),
    r.clock_offset_active::text, r.actor_type, r.actor_id, r.action, r.outcome, r.severity,
    coalesce(r.reason_code, ''), r.summary,
    coalesce(r.diff::text, ''), coalesce(r.refs::text, ''), coalesce(r.error::text, ''), coalesce(r.not_fetched::text, ''),
    coalesce(r.model, ''), coalesce(r.prompt_version, ''),
    coalesce(r.trace_id, ''), coalesce(r.span_id, ''), coalesce(r.parent_span_id, ''),
    r.prev_hash)
$$;

CREATE FUNCTION audit_chain_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE last_seq bigint; last_hash text;
BEGIN
  -- Serialise writers per chain, only for this short insert.
  PERFORM pg_advisory_xact_lock(hashtext('audit:' || NEW.chain_key));
  SELECT seq, row_hash INTO last_seq, last_hash
    FROM audit_log WHERE chain_key = NEW.chain_key ORDER BY seq DESC LIMIT 1;
  NEW.seq       := coalesce(last_seq, 0) + 1;
  NEW.prev_hash := coalesce(last_hash, 'GENESIS');
  NEW.ts        := clock_timestamp();
  NEW.row_hash  := encode(digest(audit_canonical(NEW), 'sha256'), 'hex');
  RETURN NEW;
END $$;
CREATE TRIGGER audit_chain_bi BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_chain_trigger();

CREATE FUNCTION audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP; END $$;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_immutable();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_immutable();

-- Recompute every hash and check links and contiguity. One row per chain; first_bad_seq is NULL when ok.
CREATE FUNCTION verify_audit_chain(only_chain text DEFAULT NULL)
RETURNS TABLE(chain_key text, ok boolean, checked bigint, first_bad_seq bigint, problem text, head_hash text)
LANGUAGE plpgsql STABLE AS $$
DECLARE r audit_log; cur text := NULL; expect_seq bigint; expect_prev text;
        n bigint := 0; bad bigint := NULL; why text := NULL; head text := NULL;
BEGIN
  FOR r IN SELECT * FROM audit_log a WHERE only_chain IS NULL OR a.chain_key = only_chain ORDER BY a.chain_key, a.seq LOOP
    IF cur IS DISTINCT FROM r.chain_key THEN
      IF cur IS NOT NULL THEN
        chain_key := cur; ok := bad IS NULL; checked := n; first_bad_seq := bad; problem := why; head_hash := head; RETURN NEXT;
      END IF;
      cur := r.chain_key; expect_seq := 1; expect_prev := 'GENESIS'; n := 0; bad := NULL; why := NULL;
    END IF;
    n := n + 1;
    IF bad IS NULL THEN
      IF r.seq <> expect_seq THEN bad := r.seq; why := 'sequence gap (expected ' || expect_seq || ')';
      ELSIF r.prev_hash <> expect_prev THEN bad := r.seq; why := 'prev_hash mismatch';
      ELSIF r.row_hash <> encode(digest(audit_canonical(r), 'sha256'), 'hex') THEN bad := r.seq; why := 'row_hash mismatch (row altered)';
      END IF;
    END IF;
    expect_seq := r.seq + 1; expect_prev := r.row_hash; head := r.row_hash;
  END LOOP;
  IF cur IS NOT NULL THEN
    chain_key := cur; ok := bad IS NULL; checked := n; first_bad_seq := bad; problem := why; head_hash := head; RETURN NEXT;
  END IF;
END $$;
