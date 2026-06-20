# Suim spend-rollup receiver — PRD / handoff

**For:** a Rialú Claude Code session.
**From:** Suim (Rialú project id 57). Authored 20 Jun 2026.
**Status:** Not started. The Suim side is built, tested, and *gated off* waiting for this endpoint.

---

## 1. Context

Suim is "`ps`/`top` for Claude usage across the fleet" — a usage-fact warehouse that
already collects ground-truth token/cost per project (via the `suim-client` SDK
wrapper). Its design (Suim brief 0001 §9/§10) closes the fleet's spend-cap loop like
this:

1. **Budget** lives in Rialú — already there: `projects.cost_limit_hr` (defaulted to
   `1.0` when `suim` was registered).
2. **Current spend** is fed *up* from Suim as per-project rollups.
3. **Every service** enforces against that one authoritative number.

Suim **informs**; Rialú and the services **enforce** (Suim never blocks a call —
mirrors its §13). The only missing piece is a Rialú endpoint to *receive* the rollups.
Rialú's existing cost surfaces don't fit: `/api/usage/import` is an Anthropic-Console
CSV keyed by `(date, model, api_key)`, and `/api/budget` is monthly *platform* cost in
GBP. Neither ingests per-project, per-window Claude spend. Hence this PRD.

Until it ships, Suim queues rollups locally (`rollup_outbox`) with zero data loss and
its drain is gated behind an unset `RIALU_SPEND_PATH`.

## 2. The wire contract — **pinned by Suim, do not change unilaterally**

Suim owns this shape; Rialú owns its storage and policy. Suim has an executable copy
of these expectations in `tests/test_drain_contract.py` (+ `suim/rialu.py:rollup_payload`).

**Endpoint:** `POST /api/spend`

**Request body:**
```json
{
  "rollup_key": "suim|2026-06-20T00:00:00+00:00|2026-06-20T01:00:00+00:00",
  "project_id": "suim",
  "window_start": "2026-06-20T00:00:00+00:00",
  "window_end": "2026-06-20T01:00:00+00:00",
  "cost_usd": 1.5,
  "input_tokens": 100,
  "output_tokens": 50
}
```

Field notes:
- `rollup_key` — **the idempotency key.** Deterministic: `project_id|window_start|window_end`
  (empty `project_id` for the unresolved bucket). **Rialú MUST upsert on it**, because
  Suim re-sends after a lost ack (see §4). A re-send must never double-count.
- `project_id` — a Rialú **slug** (`projects.slug`), or `null` for spend Suim could not
  attribute. **Accept-not-reject:** store an unknown/null slug as-is and reconcile later
  (mirrors Suim's pass-through philosophy). Never 4xx a valid-shaped rollup for an
  unknown slug.
- `window_start` / `window_end` — UTC ISO-8601, half-open `[start, end)`. Windows are
  non-overlapping per project, so `rollup_key` is stable.
- `cost_usd` — USD (Suim's native unit). Note Rialú's other tables use GBP/EUR; convert
  on read if you must, but **store what Suim sends** to keep the key→row mapping exact.

**Response:** any `2xx` = ack; Suim marks the rollup drained. Body is Rialú's to define.
A `>=400` (or transport error) leaves the rollup queued; Suim resumes from it next drain.

**Auth:** same as the rest of `/api/*` — Cloudflare Access for browsers, Bearer for
service callers. Suim will send `Authorization: Bearer <RIALU_TOKEN>` (and a
`Host: rialu.ie` override when hitting the Fly app directly).

## 3. What Rialú builds (Rialú's plane — your decisions)

A suggested shape; adapt to Rialú conventions (`with db() as conn:`, idempotent
migrations array in `db.py`, a router under `routers/`, CF/Bearer auth).

1. **Storage — new table `project_spend`** (idempotent migration):
   ```sql
   CREATE TABLE IF NOT EXISTS project_spend (
       rollup_key    TEXT PRIMARY KEY,        -- == project_id|window_start|window_end
       project_id    TEXT,                    -- projects.slug, or NULL (unresolved)
       window_start  TEXT NOT NULL,
       window_end    TEXT NOT NULL,
       cost_usd      REAL NOT NULL,
       input_tokens  INTEGER NOT NULL,
       output_tokens INTEGER NOT NULL,
       received_at   TEXT NOT NULL DEFAULT (datetime('now'))
   );
   CREATE INDEX IF NOT EXISTS idx_spend_project ON project_spend(project_id, window_start);
   ```
   Upsert: `INSERT … ON CONFLICT(rollup_key) DO UPDATE SET …` — that is the whole
   idempotency story.

2. **Endpoint — `routers/spend.py`**: `POST /api/spend` validating the §2 body and
   upserting. Optionally broadcast a `spend.update` over the Faire hub, like
   `_broadcast_project`.

3. **Reconciliation decision (yours).** `project_spend` is per-project ground truth from
   Suim; `anthropic_usage` is account-wide by API key from Console CSV. Recommended:
   **complement, not replace** — keep both, prefer `project_spend` for per-project views.
   Don't silently merge.

4. **Policy surface (§10).** Expose recent **$/hr per project** from `project_spend`
   windows and compare to `projects.cost_limit_hr`. Either extend
   `/api/apis/costs-by-project` or add `GET /api/spend/summary` returning
   `{project_id, spend_usd_per_hr, cost_limit_hr, over_budget}`. **Computing/exposing the
   breach is in scope; actively blocking calls is not** (services enforce; this is the
   number they read).

## 4. Idempotency / lost-ack — the case to get right

Suim marks a rollup drained only on a `2xx`. If Rialú stores the row but the ack is lost
(500, timeout), Suim keeps it queued and **re-sends the identical `rollup_key`** next
drain. Upserting on `rollup_key` makes that re-send a no-op. Suim's
`test_lost_ack_resend_is_idempotent` asserts exactly this against a stub; your live
endpoint should satisfy the same.

## 5. Out of scope

- Active blocking / call interception (Suim §13 — inform, don't enforce).
- Storing Suim's raw events — Rialú receives **rollups only**.
- Currency policy beyond storing `cost_usd` as sent.

## 6. Acceptance

- `POST /api/spend` upserts on `rollup_key`; a duplicate key does not double-count.
- Unknown/`null` `project_id` is accepted and stored, not rejected.
- Per-project `$/hr` is queryable and comparable to `cost_limit_hr`.
- Pointing Suim's `RIALU_SPEND_PATH=/api/spend` (+ `RIALU_TOKEN`) makes Suim's
  `tests/test_drain_contract.py` pass against the deployed endpoint.

## 7. Turning it on (Suim side, after deploy)

Set on the Suim Fly app: `RIALU_URL=https://rialu.ie`, `RIALU_TOKEN=<bearer>` (from the
Taisce vault), `RIALU_SPEND_PATH=/api/spend`. Suim's drain then ungates and the queued
backlog flushes on the next cycle.
