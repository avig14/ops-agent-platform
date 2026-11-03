-- Step 4: execution tracking, closure checklist, closing and management summaries.

-- How many times a closed case was reopened. It is also the "epoch" of the closure checklist: a reopened case
-- starts a fresh manual checklist, so yesterday's tick can never close today's problem.
ALTER TABLE cases ADD COLUMN reopen_count int NOT NULL DEFAULT 0;

-- Manual closure items. A person ticks each one; nothing here is ever filled in by the AI.
CREATE TABLE closure_items (
  id       bigserial PRIMARY KEY,
  case_id  text NOT NULL REFERENCES cases(id),
  epoch    int  NOT NULL,
  key      text NOT NULL CHECK (key IN ('resolution_confirmed', 'spend_recorded')),
  done_by  text NOT NULL REFERENCES users(id),
  done_at  timestamptz NOT NULL DEFAULT now(),
  note     text,
  UNIQUE (case_id, epoch, key)
);

-- Stored summaries (the reporting agent's output). `source` says whether the model or the plain template wrote it.
CREATE TABLE summaries (
  id           bigserial PRIMARY KEY,
  case_id      text REFERENCES cases(id),
  kind         text NOT NULL CHECK (kind IN ('closing', 'management')),
  content      jsonb NOT NULL,
  source       text NOT NULL CHECK (source IN ('llm', 'template')),
  metrics_hash text,
  created_by   text REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX summaries_case_idx ON summaries(case_id, id DESC);
