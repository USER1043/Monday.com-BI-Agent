# Deployment Guide for Monday.com Business Intelligence Agent

This guide covers deploying the BI Agent to Streamlit Community Cloud.

## Prerequisites

- GitHub account
- Streamlit Community Cloud account
- Monday.com API token
- Groq API key

## Step 1: Prepare Repository

1. **Initialize Git Repository**
   ```bash
   cd /home/prajankarthik/My_Files/Code/SkyLark_BI_Agent
   git init
   git add .
   git commit -m "Initial commit: Monday.com BI Agent"
   ```

2. **Create .gitignore**
   Add these files to `.gitignore`:
   ```
   .venv/
   .env
   .env.board
   .org_settings.json
   __pycache__/
   *.pyc
   .DS_Store
   ```

3. **Push to GitHub**
   ```bash
   # Create a new repository on GitHub first, then:
   git remote add origin https://github.com/YOUR_USERNAME/SkyLark_BI_Agent.git
   git branch -M main
   git push -u origin main
   ```

## Step 2: Configure Streamlit Community Cloud

1. **Go to Streamlit Community Cloud**
   - Visit https://share.streamlit.io
   - Sign in with your GitHub account

2. **Create New App**
   - Click "New app"
   - Select your GitHub repository
   - Select the `main` branch
   - Set main file path to `app.py`
   - Click "Deploy"

## Step 3: Configure Secrets

In Streamlit Community Cloud, add secrets via the dashboard:

1. Go to your app's settings
2. Click "Secrets"
3. Add the following secrets:

```toml
# Monday.com Configuration
MONDAY_API_TOKEN = "your_monday_api_token_here"
DEALS_BOARD_ID = "your_deals_board_id_here"
WORK_ORDERS_BOARD_ID = "your_work_orders_board_id_here"

# Groq API Configuration
GROQ_API_KEY = "your_groq_api_key_here"

# Data source: "auto" (default), "monday" or "excel"
DATA_SOURCE = "auto"
```

### Running without a paid Monday.com plan

The agent reads board data through `data_source.py`:

- `auto` (default): uses the Monday.com MCP when `MONDAY_API_TOKEN` is set, and falls back to
  the Excel snapshots in `Data/` if the MCP read fails (retried after 10 minutes).
- `monday`: Monday.com MCP only; errors are reported to the agent.
- `excel`: Excel snapshots only; no Monday.com secrets required.

Both sources go through the same normalizer and tools, so answers are identical for the same data.
The sidebar shows which source served the last read.

## Step 4: Run Board Setup Script (One-time)

Before the agent can function, you need to set up the Monday.com boards:

1. **Set Monday.com API Token locally**
   ```bash
   export MONDAY_API_TOKEN=your_token_here
   ```

2. **Run the setup script**
   ```bash
   source .venv/bin/activate
   python setup_boards.py \
     --deals-file "Data/Deal funnel Data.xlsx" \
     --work-orders-file "Data/Work_Order_Tracker Data.xlsx"
   ```

3. **Note the board IDs** - they will be saved to `.env.board` and need to be added to Streamlit secrets

## Step 5: Test Deployment

1. **Wait for deployment** - Streamlit will automatically deploy your app
2. **Test the live URL** - Once deployed, test with sample questions:
   - "How's our pipeline looking this quarter?"
   - "What's our weighted pipeline value?"
   - "Give me this week's leadership update"

## Step 6: Verify Functionality

Ensure the following are working:
- [ ] App loads without errors
- [ ] Monday.com connection is established
- [ ] Groq API is responding
- [ ] Tool calling works correctly
- [ ] Data normalization is functioning
- [ ] Leadership update generation works
- [ ] Quarter convention persistence works

## Troubleshooting

### Monday.com Connection Issues
- Verify API token is valid
- Check board IDs are correct
- Ensure MCP server is accessible

### Groq API Issues
- Verify API key is valid
- Check you haven't exceeded rate limits
- Ensure model name is correct

### Data Issues
- Run board setup script again if data structure changed
- Check data quality reports for anomalies
- Verify normalization logic

## Hosted App URL

Once deployed, your app will be available at:
`https://your-app-name.streamlit.app`

Replace `your-app-name` with the actual name assigned by Streamlit.
