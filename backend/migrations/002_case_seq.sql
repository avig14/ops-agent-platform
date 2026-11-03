-- Readable, gap-tolerant case ids (CASE-0001). Sequence, not max()+1, so concurrent ingests never collide.
CREATE SEQUENCE case_seq START 1;
