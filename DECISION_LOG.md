# Decision Log — Monday.com Business Intelligence Agent

*Assumptions and judgment calls made when the brief was ambiguous, per the assignment's own instruction: "if you have clarifying questions, document your assumptions and proceed." Implementation detail, setup instructions, and challenges faced live in the README — this document is decisions and reasoning only.*

---

## How We Read the Brief

The clarifying email made the evaluation criteria explicit: judged on problem-solving approach and decision-making, not feature completeness — a well-thought-out partial solution beats a larger one that can't be explained, and the goal is one meaningful end-to-end workflow. That reframed our priority order for the ~5-hour budget:

1. **Non-negotiable:** live MCP integration, the data-normalization layer, and honest handling of the messy/conflicting data — this is the actual hard problem the assignment is testing.
2. **Small addition:** clarifying questions on genuinely ambiguous query intent.
3. **First to cut under time pressure:** the leadership-update generator (explicitly optional) and breadth of query coverage — a few question types answered well beats many answered shakily.

---

## Why Deals and Work Orders Were Kept Separate

No reliable record-level key exists between the two boards. Work Orders uses a work-order/deal serial number and `WOCOMPANY` codes; Deals uses a different client-code format. Masked deal names exist on both but are non-unique and produce conflicting matches on owner, sector, and value if used as a join key. Joining on any of these would risk incorrect matches and double-counted figures — worse than not joining at all.

**Decision:** treat the boards as independent fact sources connected only by shared analytical *dimensions* (sector, owner) — not a record-level key. This allows safe comparisons like pipeline value vs. work-order receivables *by sector*, without claiming a lineage the data can't support. A query implying record-level lineage ("which work order did deal X become?") gets an honest "no reliable link exists," not a guess.

**With more time:** introduce an authoritative Deal ID on the Work Orders board, or a mapping table, to unlock real funnel-to-execution and realized-revenue analysis.

---

## How the Agent Handles "This Quarter" and "Pipeline"

Both terms are load-bearing in almost every founder query and are undefined by the source data — worth one firm convention rather than a per-query guess.

**"This quarter":** never silently assumed. The first time a query depends on it, the agent asks one clarifying question — calendar quarter, or fiscal (and if fiscal, which month the financial year starts). The answer is persisted as an org-level setting and reused for the rest of the session, not re-asked. Every answer states the exact resolved date range so a misconfiguration is visible immediately.

**"Pipeline value":** defaults to active pipeline only — `Deal Status = Open`, valued by `Masked Deal value`, placed in-period by `Tentative Close Date`. Won and Dead deals are excluded from this default. On Hold deals are excluded from the headline but shown separately, not dropped, so stalled deals stay visible. Broader views (include Won/Dead/On Hold) are available on explicit request.

---

## Tech Stack Decisions

**MCP over raw API:** the assignment allows either; MCP is Monday's standardized, maintained integration surface, so using it directly means less custom plumbing and more budget for the actually-graded parts (data resilience, BI logic).

**Model/provider:** gpt-oss-120b via the Groq API, orchestrating a tool-use loop — chosen over the Anthropic API because Anthropic's new-account trial credit is no longer available, and Groq's free tier (no credit card, ~1,000 requests/day) covers a full build-and-demo cycle at zero cost. **Trade-off accepted:** open-source models are meaningfully less reliable at multi-step tool-calling than Claude — more prone to malformed tool-call output or answering from a stale prior result instead of re-invoking a tool. Mitigated with a more directive system prompt (explicit "always call the tool for a new query, never answer from memory of a prior result") and by keeping the tool surface small and composite (few, purpose-built tools rather than many granular ones the model has to chain itself) rather than relying on the model to orchestrate complex multi-tool sequences unsupervised.

**Language & Core Libraries (Python):** Python was chosen as the bedrock for its strong ecosystem around both AI orchestration and data manipulation. The official Python `mcp` SDK robustly handles the async streamable HTTP connections required by Monday.com's hosted server. The standard `openai` package natively routes to Groq's API without custom wrapping. For data cleaning, we opted for lightweight native Python structures (`dataclasses`, dicts) over `pandas`, since the record volume per tool-call is small enough that the dependency weight and serialization complexity of DataFrames wasn't justified.

**Deployment:** Streamlit app on Streamlit Community Cloud — free, fast to stand up, gives a native chat UI with minimal code, and produces a shareable link with no server management, satisfying "testable without local setup." Traded polish/customizability for speed, which is the correct trade given the evaluation is on agent behavior, not UI craft.

---

## Trade-offs Made Within Five Hours

- **Code-side normalization over prompt-side cleaning.** A deterministic normalizer between MCP and the model (dates, currency, null-sentinels, status conflicts) instead of asking the LLM to interpret messy rows fresh each turn — more setup cost, but deterministic, testable, and doesn't re-spend tokens on the same mess repeatedly.
- **Materiality-threshold disclosure over always-conservative or always-detailed.** Small gaps are excluded and quietly footnoted; gaps large enough to swing an answer get a confirmed figure *and* a labeled estimate range. Chosen over blanket exclusion (which understates numbers) or always showing ranges (which would be noisy on trivial gaps).
- **Invalid values (e.g. negative receivables) are flagged and excluded, never auto-corrected.** We didn't attempt to guess a fix (sign error? refund?) without knowing the real billing workflow — flagging for human review was judged more honest than a plausible-looking wrong answer.
- **Status fields trusted over stage/progress fields when they conflict**, on both boards (Invoice Status over WO/Billing Status; Deal Status over Deal Stage) — picked by completeness and apparent maintenance discipline, with disagreements surfaced as anomalies rather than silently resolved one way.
- **A stage-based fallback probability for weighted pipeline**, despite it inventing a number not in the source data — judged more useful to a founder than leaving 75% of the funnel out of any weighted figure, on the condition that its use is *always* disclosed, never optional.
- **Narrow query breadth over broad shaky coverage** — depth on a handful of realistic founder questions, per the priority order above.

---

## What "Leadership Updates" Means Here

Interpreted as a dedicated conversational intent (e.g. "give me this week's leadership update") that runs the existing BI tools as a fixed bundle — pipeline health, sector performance, at-risk work orders, open data-quality issues — and returns a structured markdown brief. Built on top of existing tools rather than as new infrastructure, since this was explicitly optional and scoped to not compete for time with the core requirements. Automated distribution to any external channel was not attempted — out of scope for a read-only prototype.

---

## What We'd Improve With More Time

- Authoritative Deal ID / mapping table to link Deals and Work Orders at the record level
- More robust fuzzy-matching for sector/client name variants
- Caching to reduce repeated MCP calls within a session
- Automated tests for the normalization layer, especially the unified-status and weighted-pipeline logic, given how much judgment is embedded in them
- Hardened error handling for MCP connection failures
- Richer leadership-update formatting (exportable to PDF/slides)
