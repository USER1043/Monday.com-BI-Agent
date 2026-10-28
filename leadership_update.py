"""
Leadership Update Intent for Monday.com Business Intelligence Agent

Triggered by conversational phrasing and runs a fixed bundle of existing tools.
Per BUILD_BRIEF.md Section 7:
- Triggered by conversational phrasing ("give me this week's leadership update")
- Runs a fixed bundle of existing tools (no new tools)
- Formats a structured markdown brief
- No export/posting integration — chat output only
"""

from typing import Dict, List, Any
from datetime import datetime
from tools import (
    get_deals, get_work_orders, get_data_quality_summary,
    get_org_settings, summarize_deal_values, ToolResult,
    format_inr as _inr
)


def generate_leadership_update() -> str:
    """
    Generate a comprehensive leadership update using existing tools.
    
    This function runs a fixed bundle of BI tools and formats the results
    into a structured markdown brief covering:
    - Pipeline health
    - Sector performance
    - At-risk/anomalous work orders
    - Outstanding data-quality issues
    """
    
    markdown = "# 🚁 Skylark Drones Leadership Update\n\n"
    markdown += f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    markdown += "---\n\n"
    
    # 1. Pipeline Health
    markdown += "## 📊 Pipeline Health\n\n"
    try:
        pipeline_result = get_deals(status="Open")
        if pipeline_result.success:
            deals = pipeline_result.data
            
            if deals:
                # Calculate pipeline metrics
                summary = summarize_deal_values(deals)
                total_value = summary['total_masked_deal_value']
                weighted_value = summary['total_weighted_pipeline_value']
                deal_count = len(deals)
                
                # Stage distribution
                stage_dist = {}
                for deal in deals:
                    stage = deal.get('deal_stage') or 'Unknown'
                    stage_dist[stage] = stage_dist.get(stage, 0) + 1
                
                markdown += f"**Total Open Deals:** {deal_count}\n\n"
                markdown += f"**Total Pipeline Value:** {_inr(total_value)}\n\n"
                markdown += f"**Weighted Pipeline Value:** {_inr(weighted_value)} "
                markdown += f"({summary['weighted_pipeline_deal_count']} deals in stages A–F)\n\n"
                
                markdown += "**Pipeline by Stage:**\n\n"
                for stage, count in sorted(stage_dist.items()):
                    markdown += f"- {stage}: {count} deals\n"
                
                # Check for fallback probability usage
                fallback = summary['probability_fallback']
                if fallback['count'] > 0:
                    markdown += f"\n*Note: {fallback['count']} deals ({fallback['fraction']:.1%}) used stage-based probability fallback for weighted pipeline calculation.*\n"
            else:
                markdown += "No open deals found in pipeline.\n"
        else:
            markdown += f"Error retrieving pipeline data: {pipeline_result.error}\n"
    except Exception as e:
        markdown += f"Error calculating pipeline health: {str(e)}\n"
    
    markdown += "\n---\n\n"
    
    # 2. Sector Performance
    markdown += "## 🎯 Sector Performance\n\n"
    try:
        all_deals_result = get_deals(status="All")
        if all_deals_result.success:
            all_deals = all_deals_result.data
            
            if all_deals:
                # Calculate sector metrics
                sector_metrics = {}
                for deal in all_deals:
                    sector = deal.get('sector') or 'Unknown'
                    if sector not in sector_metrics:
                        sector_metrics[sector] = {
                            'total_deals': 0,
                            'open_deals': 0,
                            'won_deals': 0,
                            'dead_deals': 0,
                            'total_value': 0,
                            'won_value': 0
                        }
                    
                    sector_metrics[sector]['total_deals'] += 1
                    value = deal.get('masked_deal_value', 0) or 0
                    sector_metrics[sector]['total_value'] += value
                    
                    status = deal.get('deal_status', '').lower()
                    if status == 'open':
                        sector_metrics[sector]['open_deals'] += 1
                    elif status == 'won':
                        sector_metrics[sector]['won_deals'] += 1
                        sector_metrics[sector]['won_value'] += value
                    elif status == 'dead':
                        sector_metrics[sector]['dead_deals'] += 1
                
                markdown += "**Performance by Sector:**\n\n"
                for sector, metrics in sorted(sector_metrics.items(), key=lambda item: str(item[0])):
                    closed_deals = metrics['won_deals'] + metrics['dead_deals']
                    win_rate = (metrics['won_deals'] / closed_deals * 100) if closed_deals > 0 else 0
                    markdown += f"### {sector}\n"
                    markdown += f"- Total Deals: {metrics['total_deals']}\n"
                    markdown += f"- Open: {metrics['open_deals']} | Won: {metrics['won_deals']} | Dead: {metrics['dead_deals']}\n"
                    markdown += f"- Win Rate (of closed deals): {win_rate:.1f}%\n"
                    markdown += f"- Total Value: {_inr(metrics['total_value'])}\n"
                    markdown += f"- Won Value: {_inr(metrics['won_value'])}\n\n"
            else:
                markdown += "No deals data available for sector analysis.\n"
        else:
            markdown += f"Error retrieving sector data: {all_deals_result.error}\n"
    except Exception as e:
        markdown += f"Error calculating sector performance: {str(e)}\n"
    
    markdown += "\n---\n\n"
    
    # 3. At-Risk Work Orders
    markdown += "## ⚠️ At-Risk Work Orders\n\n"
    try:
        # Get work orders with potential billing issues
        all_wo_result = get_work_orders()
        if all_wo_result.success:
            work_orders = all_wo_result.data
            
            if work_orders:
                at_risk = []
                
                for wo in work_orders:
                    issues = []
                    
                    # Check for billing status issues
                    billing_status = wo.get('unified_billing_status', '').lower()
                    if 'not billed' in billing_status or 'pending' in billing_status:
                        issues.append("Unbilled")
                    
                    # Check for financial anomalies
                    amount_receivable = wo.get('amount_receivable', 0) or 0
                    amount_billed = wo.get('amount_to_billed', 0) or 0
                    
                    # Nothing left to bill but money still owed
                    if amount_receivable > 0 and amount_billed == 0:
                        issues.append("Receivable outstanding, nothing left to bill")
                    
                    # Check for old uncompleted work orders
                    invoice_date = wo.get('invoice_date')
                    if invoice_date:
                        try:
                            inv_date = datetime.strptime(invoice_date, '%Y-%m-%d')
                            days_old = (datetime.now() - inv_date).days
                            if days_old > 90 and 'not billed' in billing_status:
                                issues.append(f"Over 90 days old ({days_old} days)")
                        except ValueError:
                            pass
                    
                    if issues:
                        at_risk.append({
                            'wo_number': wo.get('work_order_number', 'Unknown'),
                            'client': wo.get('client_name', wo.get('client_code', 'Unknown')),
                            'sector': wo.get('sector', 'Unknown'),
                            'issues': issues,
                            'amount': amount_receivable
                        })
                
                if at_risk:
                    markdown += f"**Found {len(at_risk)} at-risk work orders:**\n\n"
                    
                    # Group by severity
                    high_risk = [wo for wo in at_risk if len(wo['issues']) >= 2]
                    medium_risk = [wo for wo in at_risk if len(wo['issues']) == 1]
                    
                    if high_risk:
                        markdown += "### 🔴 High Risk\n\n"
                        for wo in high_risk[:5]:  # Show top 5
                            markdown += f"**{wo['wo_number']}** - {wo['client']} ({wo['sector']})\n"
                            markdown += f"- Issues: {', '.join(wo['issues'])}\n"
                            markdown += f"- Amount receivable: {_inr(wo['amount'])}\n\n"
                    
                    if medium_risk:
                        markdown += "### 🟡 Medium Risk\n\n"
                        markdown += f"Showing {min(5, len(medium_risk))} of {len(medium_risk)} medium-risk work orders:\n\n"
                        for wo in medium_risk[:5]:
                            markdown += f"**{wo['wo_number']}** - {wo['client']}\n"
                            markdown += f"- Issue: {wo['issues'][0]}\n\n"
                    
                    shown = min(5, len(high_risk)) + min(5, len(medium_risk))
                    if len(at_risk) > shown:
                        markdown += f"*... and {len(at_risk) - shown} more at-risk work orders*\n"
                else:
                    markdown += "✅ No at-risk work orders identified.\n"
            else:
                markdown += "No work orders data available.\n"
        else:
            markdown += f"Error retrieving work orders: {all_wo_result.error}\n"
    except Exception as e:
        markdown += f"Error identifying at-risk work orders: {str(e)}\n"
    
    markdown += "\n---\n\n"
    
    # 4. Data Quality Issues
    markdown += "## 🔍 Data Quality Summary\n\n"
    try:
        deals_quality = get_data_quality_summary("deals")
        wo_quality = get_data_quality_summary("work_orders")
        
        if deals_quality.success:
            dq = deals_quality.data
            markdown += "### Deals Board\n\n"
            markdown += f"- Total Records: {dq['total_rows']}\n"
            markdown += f"- Cleaned Records: {dq['cleaned_rows']}\n"
            markdown += f"- Excluded: {dq['excluded_count']} ({dq['excluded_fraction']:.1%})\n"
            markdown += f"- Anomalies Detected: {dq['anomaly_count']}\n"
            
            if dq['anomalies_by_type']:
                markdown += "\n**Anomaly Types:**\n"
                for anomaly_type, count in dq['anomalies_by_type'].items():
                    markdown += f"- {anomaly_type}: {count}\n"
            
            if dq['sample_anomalies']:
                markdown += "\n**Sample Anomalies:**\n"
                for anomaly in dq['sample_anomalies'][:3]:
                    markdown += f"- {anomaly['detail']}\n"
        
        markdown += "\n"
        
        if wo_quality.success:
            wq = wo_quality.data
            markdown += "### Work Orders Board\n\n"
            markdown += f"- Total Records: {wq['total_rows']}\n"
            markdown += f"- Cleaned Records: {wq['cleaned_rows']}\n"
            markdown += f"- Excluded: {wq['excluded_count']} ({wq['excluded_fraction']:.1%})\n"
            markdown += f"- Anomalies Detected: {wq['anomaly_count']}\n"
            
            if wq['anomalies_by_type']:
                markdown += "\n**Anomaly Types:**\n"
                for anomaly_type, count in wq['anomalies_by_type'].items():
                    markdown += f"- {anomaly_type}: {count}\n"
            
            if wq['sample_anomalies']:
                markdown += "\n**Sample Anomalies:**\n"
                for anomaly in wq['sample_anomalies'][:3]:
                    markdown += f"- {anomaly['detail']}\n"
        
    except Exception as e:
        markdown += f"Error retrieving data quality summary: {str(e)}\n"
    
    markdown += "\n---\n\n"
    
    # 5. Organization Settings
    markdown += "## ⚙️ Current Settings\n\n"
    try:
        settings = get_org_settings()
        if settings.success:
            if settings.data:
                markdown += "**Organization Configuration:**\n\n"
                for key, value in settings.data.items():
                    markdown += f"- {key}: {value}\n"
            else:
                markdown += "No organization settings configured.\n"
        else:
            markdown += f"Error retrieving settings: {settings.error}\n"
    except Exception as e:
        markdown += f"Error retrieving settings: {str(e)}\n"
    
    markdown += "\n---\n\n"
    markdown += "*This leadership update was generated automatically using the BI Agent's analysis tools.*\n"
    
    return markdown


def is_leadership_update_request(user_message: str) -> bool:
    """
    Detect if the user is requesting a leadership update.
    
    This function checks for conversational phrasing that indicates
    a leadership update request. Only explicit requests for the brief count;
    scoped asks like "give me an update on Mining deals" go to the model.
    """
    leadership_keywords = [
        "leadership update",
        "weekly update",
        "executive update",
        "management update"
    ]
    
    user_message_lower = user_message.lower()
    return any(keyword in user_message_lower for keyword in leadership_keywords)


if __name__ == "__main__":
    # Test the leadership update generation
    print("Generating leadership update...")
    update = generate_leadership_update()
    print(update)
