# Build Brief — Monday.com Business Intelligence Agent

For coding agents. All architectural and business-logic decisions referenced here are final — see `DECISION_LOG.md` for the "why." This document is the "what to build." Do not re-litigate a decision that's already made; if something genuinely wasn't anticipated, flag it back rather than silently deciding.

---

## 0. Build Order

Build and verify in this order — each stage should be runnable/testable before moving to the next:

1. Board setup script (one-time, offline from the agent)
2. Normalization layer (pure functions, unit-testable, no LLM/MCP involved)
3. MCP connection + purpose-built tool wrappers
4. Agent loop (Claude + tools) — text in/out, no UI yet
5. Streamlit chat UI
6. Leadership-update intent (only after 1–5 are solid)
7. Deploy

If time runs out, stop after the stage you're on and make sure it's solid — do not leave stage 4 half-working to start stage 6. Priority order is in `DECISION_LOG.md` §"How We Read the Brief."

---

## 1. Board Setup Script

One-time script, run manually, not part of the live agent. Reads the two source files and creates the two Monday.com boards via API (setup script may use the raw Monday API directly — the "MCP for integration" decision applies to the *agent's* runtime reads, not to one-time provisioning).

- Create `Deals` board and `Work Orders` board with column types matching the data (status/dropdown columns for categorical fields, date columns, number columns for currency/quantity, text for names/codes).
- Apply the **structural fixes** below during import, not left for the runtime normalizer to fix repeatedly:
  - Skip the empty header row / offset row on Work Orders import (real headers are one row down).
  - Drop the two embedded sub-header rows on Deals (rows where `Deal Status == 'Deal Status'`).
  - Drop exact duplicate rows on Deals (12 expected).
  - Drop the 4 fully-empty columns on Work Orders (Expected Billing Month, Actual Collection Month, Collection status, Collection Date) — do not create these columns on the board at all.
- Do **not** pre-clean values that the runtime normalizer is responsible for (currency strings, embedded units, typos like `BIlled`) — import as-is so the normalization layer has real messy data to work against and is actually exercised/testable.
- Output: board IDs, saved to config/env for the agent to reference.

---

## 2. Normalization Layer

Pure Python, no LLM calls, fully unit-testable in isolation. Takes raw MCP item data in, returns cleaned records + a structured data-quality report out. This is the module most worth writing tests for.

### 2.1 Generic normalization (applies broadly)
- Null-sentinel handling: treat `""`, `"N/A"`, `"TBD"`, `"-"`, `"#VALUE!"`, and similar as missing, uniformly.
- Currency/number coercion: strip currency symbols/commas, coerce to float; on failure, treat as missing (never silently zero).
- Date parsing: normalize to ISO 8601 regardless of input format.
- Text normalization: trim whitespace, fix known casing typos (e.g. `BIlled` → `Billed`) before any downstream logic touches the field.
- Multi-select field order-independence (e.g. `Type of Work`): normalize by sorting/canonicalizing the label set so `"A, B"` and `"B, A"` group identically.
- Embedded-unit fields (e.g. `Quantities as per PO`): regex-split into `quantity_value` (float) and `quantity_unit` (string category). Rows that don't parse cleanly → `quantity_value = None`, flagged, not dropped.
- Drop/ignore any column with ~100% nulls before it reaches the LLM's context at all (see setup script — ideally these never even exist as columns).

### 2.2 Unified Billing Status (Work Orders) — per Decision Log
Precedence: `Invoice Status` → `WO Status (billed)` (fill nulls only) → `Billing Status` (last resort, nulls only). Implement as:
```
unified_billing_status = invoice_status if not null(invoice_status)
                        else wo_status if not null(wo_status)
                        else billing_status if not null(billing_status)
                        else "Unknown"
```
Separately, detect and flag **contradictions**: if a higher-precedence field and a lower-precedence field are both present and disagree (e.g. `WO Status = Closed` but `Invoice Status = Not billed yet`), emit a data-quality anomaly entry for that row — this is independent of which value gets used for `unified_billing_status`.

### 2.3 Deal Status vs. Deal Stage (Deals) — per Decision Log
`Deal Status` is authoritative for Won/Dead/Open classification everywhere. `Deal Stage` is never used to override it. When they disagree (e.g. Status=Won but Stage=A. Lead Generated), flag as anomaly; still report `Deal Status` as the deal's state.

### 2.4 Invalid value handling (both boards) — per Decision Log
Financial-invariant violations (negative `Amount to be billed`, negative `Amount Receivable`) are:
- Excluded from clean aggregates (same as missing), **and**
- Always emitted as a named anomaly regardless of row count — this bypasses the materiality threshold below; invalid values are never "too small to mention."
- Never auto-corrected (no abs(), no clipping to zero).

### 2.5 Materiality-threshold disclosure (missing/malformed data in calculations)
Given a query's matched row set and a target field:
- Compute `excluded_fraction = excluded_rows / matched_rows`.
- If `excluded_fraction` is small (suggested default: ≤10%) → return the clean-rows-only figure + `excluded_count` for a quiet footnote. No range.
- If `excluded_fraction` exceeds the threshold → return **both** the clean-rows-only figure ("confirmed") and an estimated upper bound using a simple, stated method (e.g. average value of the clean subset applied to the excluded rows), clearly labeled as an estimate, plus the reason for the gap.
- This threshold should be a named constant, not a magic number buried in logic — coding agents should make it easy to find/tune.

### 2.6 Weighted pipeline fallback probability (Deals) — per Decision Log
Applies only to deals with `Deal Status = Open` and stage in A–F. Deals in stages G–K or with Status = Won/Dead are excluded entirely from weighted-pipeline figures (not assigned 0%/100%, just out of scope for this metric).

```
STAGE_PROBABILITY = {
    "A. Lead Generated": 0.10,
    "B. Sales Qualified Leads": 0.20,
    "C. Demo Done": 0.35,
    "D. Feasibility": 0.45,
    "E. Proposal/Commercials Sent": 0.60,
    "F. Negotiations": 0.80,
}
```
Use `Closure Probability` directly when present; fall back to `STAGE_PROBABILITY[deal_stage]` only when it's missing. Any tool output that includes a weighted-pipeline figure MUST include a flag/field stating how many/what fraction of included deals used the fallback — this disclosure is mandatory, not subject to the materiality threshold in 2.5.

### 2.7 Aggregation defaults (Deals)
Default to **median** for "typical deal size" type questions, given extreme right-skew (mean ≈ 12.7× median in the audit). If a tool also returns a mean, pair it with the median so skew is visible rather than presenting mean alone.

### 2.8 Surrogate key (Deals)
Generate `deal_id = hash(client_code + deal_name + created_date)` at ingestion/normalization time — `Deal Name` alone is not unique (repeats up to 27×) and cannot be used for grouping or dedup.

### 2.9 Data-quality report shape
Every normalizer function that processes a batch of rows should return, alongside the cleaned data, something like:
```python
{
  "cleaned_records": [...],
  "excluded_count": int,
  "excluded_fraction": float,
  "anomalies": [
    {"row_id": ..., "type": "status_contradiction" | "invalid_value" | "stage_status_mismatch" | ...,
     "detail": "human-readable one-liner"}
  ]
}
```
This structure is what the agent's tools return to Claude — it should never need to re-derive data quality from raw rows itself.

---

## 3. MCP Integration

- Connect to Monday.com's official MCP server as the read layer (per Decision Log — no custom GraphQL wrapper duplicating MCP's own board/item-read tools).
- Auth via Monday.com API token, loaded from environment/config — never hardcoded.
- Handle connection failures gracefully: if MCP is unreachable or auth fails, the agent should say so plainly to the user rather than hanging or returning a generic error.

## 4. Purpose-Built Tools (wrapping MCP + the normalizer)

Expose these to Claude, not raw MCP calls directly, so the model's job is business reasoning, not data wrangling:

- `get_deals(filters)` — sector, stage, status, date range, owner. Returns normalized records + quality report (§2.9).
- `get_work_orders(filters)` — sector, unified billing status, date range, client. Returns normalized records + quality report.
- `get_data_quality_summary(board)` — on-demand full anomaly/sparsity report for a board, for when the user directly asks about data quality.
- `get_org_settings()` / `set_org_setting(key, value)` — small persistence for the fiscal-quarter convention (§ below). Simplest possible implementation (local file/session state) is fine for a prototype; don't over-build this.

Each tool's docstring/schema should be explicit about units, field meanings, and any default scoping (e.g. `get_deals` defaults to `Deal Status = Open` unless the caller specifies otherwise) so Claude doesn't have to guess from field names alone.

## 5. Agent Loop / System Prompt

**Model:** Llama 3.3 70B via Groq (OpenAI-compatible tool-calling format, `https://api.groq.com/openai/v1`). Free tier: ~30 req/min, ~1,000 req/day, ~12K TPM, ~100K TPD — plenty for building/testing/demoing, but design the loop to avoid unnecessary round-trips per query (favor the composite tools in §4 over many small chained calls).

**Test tool-calling reliability early — before building anything on top of it.** Open-source models are noticeably less disciplined than Claude at multi-step tool orchestration: they can emit malformed tool-call JSON, skip calling a tool and answer from a guess instead, or answer a new query using a stale prior tool result rather than re-invoking. Before writing the rest of the agent loop, run a handful of representative queries (including follow-up queries that change the filter) and confirm the model actually re-calls tools when it should. If it doesn't reliably do so, harden the system prompt with explicit, blunt instructions (e.g. "You MUST call the relevant tool for every new question involving deals or work orders — never answer from a previous tool result, even if the question looks similar") before moving on — don't assume prompt-level discipline transfers from how Claude behaves.

The system prompt should teach, once, rather than relying on the model to infer from raw data:
- The domain vocabulary: sector list, deal stage list (in order), unified billing status values — pull these from the actual board schema once boards exist.
- The quarter-convention behavior (§ below).
- The pipeline default-scope behavior (Decision Log).
- Instruction to always surface anomalies/disclosures returned by tools rather than silently dropping them from the answer.
- Instruction to ask **one** clarifying question when query intent/scope is genuinely ambiguous (sector doesn't map, unclear date range, "pipeline" meaning value vs. count) — and to state an assumption and proceed for everything else.
- Instruction to never claim a Deals↔Work Orders record-level relationship (Decision Log) — dimension-level comparisons only.

### Quarter convention (stateful, asked once)
- On the first query where "quarter" (or similar relative period language) matters, ask: calendar quarter, or fiscal (if fiscal, which month does the FY start)?
- Persist the answer via `set_org_setting`.
- On every subsequent quarter-dependent query, read the setting via `get_org_settings()` and use it silently — do not re-ask.
- Every answer that resolves "this quarter" to a date range should state that range explicitly in the response.

## 6. Streamlit UI

- Standard chat interface (`st.chat_message` / `st.chat_input`).
- Persist conversation + org settings (quarter convention) in `st.session_state` for the session.
- Nothing fancier is required — this is not where evaluation weight sits (Decision Log, Tech Stack).

## 7. Leadership Update Intent (build last, cut first if short on time)

- Triggered by conversational phrasing ("give me this week's leadership update" or similar) — not a separate UI element.
- Runs a fixed bundle of the existing tools (§4) — no new tools — and formats a structured markdown brief: pipeline health, sector performance, at-risk/anomalous work orders, outstanding data-quality issues.
- No export/posting integration — chat output only.

## 8. Deployment

- Push to GitHub, deploy via Streamlit Community Cloud pointed at the repo.
- Secrets (Anthropic API key, Monday API token/MCP config) via Streamlit's secrets manager — never committed.
- Confirm the deployed link is publicly accessible and testable with no local setup before calling this done.

---

## Definition of Done (minimum, per priority order in Decision Log)

Even if 6/7/leadership-update don't get built, the following must work end-to-end for the submission to be defensible:
- A live Monday.com read (via MCP) that reflects the actual board data, not cached/mocked.
- At least 3 realistic founder questions answered correctly, with correct data-quality disclosures where relevant (e.g. a weighted-pipeline question showing the fallback-probability disclosure; a billing question reflecting the unified status logic).
- The quarter-convention ask-once-then-persist behavior demonstrably working.
- Anomalies (status contradictions, invalid values) visible in at least one answer, not silently dropped.
