-- Region scoping for cases: starts as the requester's region, replaced by the store's region once the store is resolved.
ALTER TABLE cases ADD COLUMN region text;
CREATE INDEX cases_region_idx ON cases(region);
