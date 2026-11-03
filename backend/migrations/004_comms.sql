-- Step 3: sender loop, sandbox mailbox, Comms agent drafts.

ALTER TABLE outbox
  ADD COLUMN max_attempts int NOT NULL DEFAULT 5,                                    -- attempt budget; a manual retry raises it
  ADD COLUMN audience     text NOT NULL DEFAULT 'internal' CHECK (audience IN ('internal','external')),
  ADD COLUMN reason_code  text,                                                      -- last failure code (COMM_*)
  ADD COLUMN source       text NOT NULL DEFAULT 'template' CHECK (source IN ('template','llm')),
  ADD COLUMN reviewed_by  text REFERENCES users(id),
  ADD COLUMN reviewed_at  timestamptz,
  ADD COLUMN edited       boolean NOT NULL DEFAULT false;

-- Invariant enforced by the database itself: an external message can only leave the outbox after a human released it.
ALTER TABLE outbox ADD CONSTRAINT outbox_external_needs_review
  CHECK (audience = 'internal' OR status IN ('draft','cancelled') OR reviewed_by IS NOT NULL);

CREATE INDEX outbox_case_idx ON outbox(case_id, id);

-- The sandbox "mail provider". message_id is UNIQUE, so receiving the same message twice stores it once:
-- at-least-once sending (retries, lost acks, crashed workers) can never show the reader a duplicate.
CREATE TABLE mailbox (
  id           bigserial PRIMARY KEY,
  message_id   text NOT NULL UNIQUE,
  outbox_id    bigint,
  case_id      text REFERENCES cases(id),
  kind         text NOT NULL,
  recipient    text NOT NULL,
  subject      text,
  body         text NOT NULL,
  channel      text NOT NULL DEFAULT 'sandbox',
  delivered_at timestamptz NOT NULL DEFAULT now(),
  read_at      timestamptz
);
CREATE INDEX mailbox_recipient_idx ON mailbox(lower(recipient), id DESC);
CREATE INDEX mailbox_case_idx ON mailbox(case_id);
