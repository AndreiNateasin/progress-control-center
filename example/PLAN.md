# Demo Project — plan

A tiny plan so you can see the dashboard with something in it. Delete this
directory once you have adopted a real project.

### Phase 1 — Ingest pipeline

- [x] Decide the input format
- [x] Parse the vendor feed into records
- [ ] Reject malformed rows with a reason
- [ ] Backfill the last 30 days

### Phase 2 — Serve it

- [ ] HTTP layer with a health endpoint
- [ ] Pagination
- [ ] Rate limiting

### Phase 3 — Watch it

- [ ] Metrics for ingest lag
- [ ] Alert when the feed goes stale
