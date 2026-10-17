# past.dev G-PD1 to G-PD3: approval request (Mission 004C)

Status: **prepared, not approved.** Nothing has been sent to past.dev, no project or key exists for amem, no
credit has been spent. Code is ready on `rsi/m004c` (G-PD0 correctness fixes, F2/F3/F5/F8 egress fixes).

## Configuration (owner's V1 terms)

- One dedicated project `amem-rsi` (Pay as you go allows one project; archived ones count).
- Monthly project caps in Settings > Projects: **credit cap 250**, **ingestion cap 100 data points**.
  A reached cap answers `429 project-cap-reached`; amem stops the run as `capped` and does not retry.
- One project-scoped key (`past_sk_...`). No management key.
- Synthetic records only; no transcripts, paths, secrets, emails, employer or private-repo data.
- No automatic sync: every run is a manual `amem sync run --sink pastdev` in this mission.

## Steps

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-PD1 | owner | Create project `amem-rsi`; set credit cap 250 and ingestion cap 100 per month; set an account spend cap if the console offers one; create one project key. Tell the agent if a project already existed. | caps visible in the console | delete the project (revokes its keys, drops the 7-day request-log payloads) |
| G-PD2 | agent (popup) | `request_secret` `AMEM_PASTDEV_KEY` into the 004B worktree `.env.local`; health check and one `GET /usage` (both free). | health ok; usage shows the caps and 0 used | remove the key |
| G-PD3 | agent | C3 on a throwaway Postgres seeded with `amem synth seed` (see below). | every check below | `delete_all`, then G-PD1 undo |

## C3 checks (synthetic)

Dataset: `amem synth seed` decision records: 8 to send, 2 team and 2 private that must be refused.
1. `amem sync plan --sink pastdev --json`: 8 send, 4 refused (`team`, `private`); free `estimate` pre-flight
   equals the local credit estimate.
2. `amem sync run --sink pastdev`: each ingestion reaches `completed` within 180 s; links store the
   `ingestionId`; each returned `sourceId` equals our `amem:` ref byte for byte.
3. Recall: 3 synthetic queries return refs that re-read in Supabase.
4. Duplicates: a second run sends nothing; `--replay` ingests nothing new and costs 0; one changed record
   costs exactly its estimate.
5. Deletion: `delete_all` answers 202; recall no longer returns the ids; the ids are retired and a later run
   does not re-send them.
6. Latency p50/p95 for ingest and recall; `/usage` delta within 10% of the estimate.
7. Leak scan: 0 planted markers in our request bodies, in the stored payloads read back from `/requests`,
   and in recall output.

## Cost

- 1 credit per 350 bytes per data point (rounded up), records at most 1.5 KB: 8 records is at most 40
  credits; about 30 recall calls at 0.1 is 3; deletes, retries, polls, estimate and usage are free.
- Expected 15-45 credits; hard stop at 250 (project cap); 0.03% of the ~150,000 signup credits.

## Not included

Real historical records (G-PD5), the recall benchmark vs Supabase (G-PD4, needs an owner-reviewed set of
15 queries), any schedule or automatic sync.

## Owner questions

1. Does the past.dev org already have a project (it would block a new one)?
2. Is an account-level spend cap available? If yes, set it to the same 250.
3. Region, subprocessors and DPA are "on request": accept that for synthetic data only?
4. After C3: delete the project, or keep it empty for a later G-PD5?
