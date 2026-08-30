# Monday.com Business Intelligence Agent

*Skylark Drones — Technical Assignment*

> For assumptions, trade-offs, and judgment calls made under ambiguity, see [DECISION_LOG.md](./DECISION_LOG.md). This document covers what the app does, how it's built, and how to run it.

---

## What the App Does

The Monday.com Business Intelligence Agent is a conversational AI assistant that helps founders analyze sales pipeline and work order execution data. It connects to two Monday.com boards (Deals and Work Orders), automatically normalizes messy data, handles data quality issues transparently, and provides intelligent business insights through natural language queries.

The agent features:
- **Intelligent Query Processing**: Ask questions about pipeline value, sector performance, billing status, and more
- **Automatic Data Normalization**: Handles currency strings, date formats, null values, and embedded units
- **Data Quality Awareness**: Surfaces anomalies, contradictions, and data gaps in every response
- **Quarter Convention Support**: Remembers fiscal vs calendar quarter preferences per session
- **Leadership Updates**: Generate comprehensive business summaries on demand
- **No Record-Level Linking**: Safely compares boards by shared dimensions only

---

## Architecture and Tech Stack

**Architecture**: Streamlit frontend → Groq API agent/tool-use loop using Llama 3.3 70B → normalization layer → Monday.com MCP server → Monday.com board

- **Frontend**: Streamlit chat interface with session state persistence
- **Orchestration**: Llama 3.3 70B via Groq API with OpenAI-compatible tool calling
- **Integration layer**: Monday.com official MCP server for board/item reads
- **Data normalization layer**: Pure Python functions for deterministic data cleaning
- **Hosting**: Streamlit Community Cloud (free tier)

---

## How to Configure Monday.com

### Step 1: Obtain Monday.com API Token
1. Go to Monday.com account settings
2. Navigate to "Developers" → "API tokens"
3. Create a new token with read permissions
4. Save the token securely

### Step 2: Run Board Setup Script
The board setup script creates the Monday.com boards and imports data:

```bash
export MONDAY_API_TOKEN=your_token_here
source .venv/bin/activate
python setup_boards.py \
  --deals-file "Data/Deal funnel Data.xlsx" \
  --work-orders-file "Data/Work_Order_Tracker Data.xlsx"
```

This script:
- Creates Deals and Work Orders boards with appropriate column types
- Applies structural fixes (removes empty rows, duplicates, null columns)
- Imports data while preserving messy values for normalization testing
- Saves board IDs to `.env.board` for agent configuration

### Board Schema

**Deals board:**
- Deal Name (text)
- Client Code (text)
- Sector (text)
- Deal Status (status)
- Deal Stage (status)
- Owner (text)
- Created Date (date)
- Tentative Close Date (date)
- Masked Deal value (numbers)
- Amount to be billed (numbers)
- Amount Receivable (numbers)
- Closure Probability (text)
- WOCOMPANY (text)
- Work Order/Deal Serial No (text)

**Work Orders board:**
- Work Order Number (text)
- Client Code (text)
- Sector (text)
- Client Name (text)
- Type of Work (text)
- Quantities as per PO (text)
- Amount to be billed (numbers)
- Amount Receivable (numbers)
- Invoice Status (status)
- WO Status (billed) (status)
- Billing Status (status)
- Invoice Date (date)
- Work Order Date (date)
- Owner (text)

---

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| MONDAY_API_TOKEN | Monday.com API token for board access | Yes |
| DEALS_BOARD_ID | Board ID for Deals board (from setup script) | Yes |
| WORK_ORDERS_BOARD_ID | Board ID for Work Orders board (from setup script) | Yes |
| GROQ_API_KEY | Groq API key for Llama 3.3 70B access | Yes |

For local development, create a `.env` file based on `.env.example`. For Streamlit Cloud deployment, add these as secrets in the app settings.

---

## Local Setup and Deployment

### Local Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/SkyLark_BI_Agent.git
   cd SkyLark_BI_Agent
   ```

2. **Create virtual environment**
   ```bash
   uv venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   uv pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual API keys and board IDs
   ```

5. **Run the board setup script** (one-time)
   ```bash
   python setup_boards.py \
     --deals-file "Data/Deal funnel Data.xlsx" \
     --work-orders-file "Data/Work_Order_Tracker Data.xlsx"
   ```

6. **Run the app**
   ```bash
   streamlit run app.py
   ```

### Deployment

See [deploy.md](./deploy.md) for detailed deployment instructions to Streamlit Community Cloud.

**Hosted app:** (Link will be added after deployment)

---

## How to Test It

### Sample Questions to Try

- "How's our pipeline looking for Agriculture this quarter?"
- "What's our weighted pipeline value right now?"
- "Which work orders are at risk of billing issues?"
- "Show me deals in the Negotiations stage"
- "What's our data quality status?"
- "Give me this week's leadership update"

### What to Expect

**Data Quality Disclosures:**
- Every response includes data quality information when relevant
- Small data gaps (<10%) are footnoted quietly
- Large gaps show both confirmed figures and labeled estimates
- Anomalies (status contradictions, invalid values) are always surfaced

**Clarifying Question Behavior:**
- The agent asks one clarifying question when query intent is genuinely ambiguous
- Examples: "calendar quarter, or fiscal (and if fiscal, which month does the FY start)?"
- The answer is persisted for the session and not re-asked
- All other queries proceed with stated assumptions

**Quarter Convention:**
- First quarter-dependent query triggers the convention question
- The resolved date range is always stated in the response
- Settings persist via `set_org_setting` for the session

---

## AI Tools Used

**Devin (Cognition AI Agent)**
- Used for code generation, file management, and task orchestration
- Implemented the complete BUILD_BRIEF.md specification
- Created all components from board setup to deployment

---

## Known Limitations

**From Decision Log:**
- No reliable record-level key exists between Deals and Work Orders boards
- Stage-based fallback probability for weighted pipeline (always disclosed)
- Open-source models (Llama 3.3) are less reliable at multi-step tool calling than Claude
- No automated distribution of leadership updates (chat output only)

**Implementation Constraints:**
- MCP server integration assumes Monday.com's official MCP server is available
- Rate limits on Groq API may affect usage during heavy testing
- No caching layer for repeated MCP calls within a session
- Limited error recovery for MCP connection failures

---

## Challenges Faced

**Implementation Challenges:**
- **MCP Server Availability**: Monday.com's official MCP server integration requires proper configuration and may have availability constraints
- **Tool-Calling Reliability**: Open-source models require more directive prompts to ensure reliable tool invocation
- **Data Structure Complexity**: Real-world data contains embedded units, conflicting status fields, and various null representations
- **Time Budget**: Prioritized core functionality (MCP integration, normalization, tool calling) over extensive query coverage

**Data Quality Challenges:**
- **Status Contradictions**: Invoice Status vs WO Status vs Billing Status conflicts require precedence logic
- **Financial Invariants**: Negative amounts need special handling (flagged, never auto-corrected)
- **Missing Probability Data**: 75% of deals lack Closure Probability, requiring stage-based fallback
- **Embedded Units**: Quantities mixed with units require regex parsing and error handling

---

## Future Improvements

**With More Time:**
- Authoritative Deal ID / mapping table to link Deals and Work Orders at record level
- More robust fuzzy-matching for sector/client name variants
- Caching to reduce repeated MCP calls within a session
- Automated tests for normalization layer logic
- Hardened error handling for MCP connection failures
- Richer leadership-update formatting (exportable to PDF/slides)
- Support for additional query types and broader question coverage
