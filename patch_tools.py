import re

with open("tools.py", "r") as f:
    content = f.read()

new_tools_code = """
def get_deal_metrics(
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    owner: Optional[str] = None,
    group_by: Optional[str] = None
) -> ToolResult:
    \"\"\"
    Get aggregated deal metrics (sums, counts) with optional filtering and grouping.
    \"\"\"
    # Re-use get_deals logic but only return metrics
    result = get_deals(sector, stage, status, date_from, date_to, owner)
    if not result.success:
        return result
        
    records = result.data
    
    def _calc(recs):
        return {
            "count": len(recs),
            "total_masked_deal_value": sum(float(r.get('masked_deal_value') or 0) for r in recs),
            "total_weighted_pipeline_value": sum(float(r.get('masked_deal_value') or 0) * float(r.get('stage_probability') or 0) for r in recs)
        }
        
    if not group_by:
        metrics = _calc(records)
    else:
        grouped = {}
        for r in records:
            val = r.get(group_by, "Unknown")
            if val not in grouped:
                grouped[val] = []
            grouped[val].append(r)
        metrics = {k: _calc(v) for k, v in grouped.items()}
        
    return ToolResult(
        success=True,
        data={"metrics": metrics},
        quality_report=result.quality_report,
        metadata={"filters_applied": result.metadata.get("filters_applied")}
    )

def get_work_order_metrics(
    sector: Optional[str] = None,
    billing_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    client: Optional[str] = None,
    group_by: Optional[str] = None
) -> ToolResult:
    \"\"\"
    Get aggregated work order metrics (sums, counts) with optional filtering and grouping.
    \"\"\"
    result = get_work_orders(sector, billing_status, date_from, date_to, client)
    if not result.success:
        return result
        
    records = result.data
    
    def _calc(recs):
        return {
            "count": len(recs),
            "total_amount_to_be_billed": sum(float(r.get('amount_to_billed') or 0) for r in recs),
            "total_amount_receivable": sum(float(r.get('amount_receivable') or 0) for r in recs)
        }
        
    if not group_by:
        metrics = _calc(records)
    else:
        grouped = {}
        for r in records:
            val = r.get(group_by, "Unknown")
            if val not in grouped:
                grouped[val] = []
            grouped[val].append(r)
        metrics = {k: _calc(v) for k, v in grouped.items()}
        
    return ToolResult(
        success=True,
        data={"metrics": metrics},
        quality_report=result.quality_report,
        metadata={"filters_applied": result.metadata.get("filters_applied")}
    )

"""

# Insert before get_data_quality_summary
content = content.replace("def get_data_quality_summary", new_tools_code + "\ndef get_data_quality_summary")

# Add definitions
deal_metrics_def = """    {
        "type": "function",
        "function": {
            "name": "get_deal_metrics",
            "description": "Get aggregated mathematical metrics for deals (e.g. total value, weighted pipeline, counts). Supports filtering and optional grouping (e.g. group_by='sector'). Use this instead of get_deals when the user asks for sums, totals, or aggregates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "stage": {"type": ["string", "null"], "description": "Filter by deal stage"},
                    "status": {"type": ["string", "null"], "description": "Filter by deal status (defaults to 'Open' if not specified)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by tentative close date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by tentative close date to (ISO format YYYY-MM-DD)"},
                    "owner": {"type": ["string", "null"], "description": "Filter by owner"},
                    "group_by": {"type": ["string", "null"], "description": "Field to group results by (e.g., 'sector', 'deal_stage', 'owner')"}
                }
            }
        }
    },"""

wo_metrics_def = """    {
        "type": "function",
        "function": {
            "name": "get_work_order_metrics",
            "description": "Get aggregated mathematical metrics for work orders (e.g. total amount to be billed, receivable, counts). Supports filtering and grouping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "billing_status": {"type": ["string", "null"], "description": "Filter by unified billing status"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by invoice date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by invoice date to (ISO format YYYY-MM-DD)"},
                    "client": {"type": ["string", "null"], "description": "Filter by client name or code"},
                    "group_by": {"type": ["string", "null"], "description": "Field to group results by (e.g., 'sector', 'unified_billing_status')"}
                }
            }
        }
    },"""

content = content.replace("TOOL_DEFINITIONS = [", "TOOL_DEFINITIONS = [\n" + deal_metrics_def + "\n" + wo_metrics_def)

# Add to TOOL_FUNCTIONS
funcs_replacement = """TOOL_FUNCTIONS = {
    "get_deals": get_deals,
    "get_deal_metrics": get_deal_metrics,
    "get_work_orders": get_work_orders,
    "get_work_order_metrics": get_work_order_metrics,"""
content = content.replace('TOOL_FUNCTIONS = {\n    "get_deals": get_deals,\n    "get_work_orders": get_work_orders,', funcs_replacement)

with open("tools.py", "w") as f:
    f.write(content)
