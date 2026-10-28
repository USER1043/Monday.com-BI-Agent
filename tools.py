"""
Purpose-Built Tools for Monday.com Business Intelligence Agent

Wrappers around the data source (Monday.com MCP or Excel fallback) + normalizer that expose business-logic tools to Claude.
Per BUILD_BRIEF.md Section 4:
- get_deals(filters) — sector, stage, status, date range, owner
- get_work_orders(filters) — sector, unified billing status, date range, client
- get_data_quality_summary(board) — on-demand full anomaly/sparsity report
- get_org_settings() / set_org_setting(key, value) — persistence for fiscal-quarter convention
"""

import contextvars
import json
import os
import statistics
from contextlib import contextmanager
from typing import Dict, List, Any, Optional, Union
from datetime import datetime, date
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from normalization import (
    normalize_deals_batch, normalize_work_orders_batch, 
    DataQualityReport, AnomalyType, drop_null_columns,
    calculate_materiality_disclosure, MATERIALITY_THRESHOLD, STAGE_PROBABILITY
)
from data_source import (
    get_deals_data as sync_get_deals_data,
    get_work_orders_data as sync_get_work_orders_data,
    get_board_schemas as sync_get_board_schemas,
)


@dataclass
class DealFilters:
    """Filters for get_deals tool."""
    sector: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None  # Defaults to "Open" if not specified
    date_from: Optional[str] = None  # ISO format YYYY-MM-DD
    date_to: Optional[str] = None    # ISO format YYYY-MM-DD
    owner: Optional[str] = None


@dataclass
class WorkOrderFilters:
    """Filters for get_work_orders tool."""
    sector: Optional[str] = None
    billing_status: Optional[str] = None  # Uses unified billing status
    date_from: Optional[str] = None  # ISO format YYYY-MM-DD
    date_to: Optional[str] = None    # ISO format YYYY-MM-DD
    client: Optional[str] = None


@dataclass
class ToolResult:
    """Standard result format for all tools."""
    success: bool
    data: Union[List[Dict[str, Any]], Dict[str, Any]]
    quality_report: Optional[DataQualityReport] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class OrgSettings:
    """
    Organization-level settings like fiscal quarter convention.
    With settings_file=None the settings live in memory only (one store per app session).
    """
    
    def __init__(self, settings_file: Optional[str] = ".org_settings.json"):
        self.settings_file = settings_file
        self._settings = self._load_settings()
    
    def _load_settings(self) -> Dict[str, Any]:
        """Load settings from file."""
        if self.settings_file and os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}
    
    def _save_settings(self):
        """Save settings to file."""
        if not self.settings_file:
            return
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self._settings, f, indent=2)
        except IOError:
            pass
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        return self._settings.get(key, default)
    
    def set_setting(self, key: str, value: Any):
        """Set a setting value."""
        self._settings[key] = value
        self._save_settings()
    
    def get_all_settings(self) -> Dict[str, Any]:
        """Get all settings."""
        return self._settings.copy()
    
    def clear(self):
        """Remove all settings."""
        self._settings = {}
        self._save_settings()


# File-backed default for CLI/scripts; the Streamlit app gives each session its own store
org_settings = OrgSettings()

# The store get_org_settings/set_org_setting act on (see use_org_settings)
_active_org_settings: contextvars.ContextVar[OrgSettings] = contextvars.ContextVar(
    "active_org_settings", default=org_settings
)


@contextmanager
def use_org_settings(store: OrgSettings):
    """Route get_org_settings/set_org_setting to `store` for the duration of the block."""
    token = _active_org_settings.set(store)
    try:
        yield store
    finally:
        _active_org_settings.reset(token)


# Keys holding rupee amounts in normalized records and tool aggregates
MONEY_FIELDS = {
    "masked_deal_value", "amount_to_billed", "amount_receivable",
    "total_masked_deal_value", "median_deal_value", "mean_deal_value",
    "total_weighted_pipeline_value", "total_amount_to_be_billed", "total_amount_receivable",
    "value", "confirmed_value", "estimated_upper_bound",
}


def format_inr(value: float) -> str:
    """Format rupees as Indian finance teams read them: crore, lakh, or plain rupees."""
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1e7:
        return f"{sign}₹{amount / 1e7:,.2f} Cr"
    if amount >= 1e5:
        return f"{sign}₹{amount / 1e5:,.2f} L"
    return f"{sign}₹{amount:,.0f}"


def format_money_fields(obj: Any) -> Any:
    """
    Replace rupee amounts with formatted strings before they reach the model, so
    it copies figures instead of converting units itself (it slips by 10x).
    """
    if isinstance(obj, dict):
        return {
            k: format_inr(v) if k in MONEY_FIELDS and isinstance(v, (int, float)) and not isinstance(v, bool)
            else format_money_fields(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [format_money_fields(item) for item in obj]
    return obj


# Status values that mean "don't filter on status"
ALL_STATUSES = {"all", "any", "*"}

# group_by names the model may use, mapped to normalized record fields
DEAL_GROUP_FIELDS = {
    "sector": "sector", "stage": "deal_stage", "deal_stage": "deal_stage",
    "status": "deal_status", "deal_status": "deal_status", "owner": "owner",
    "client": "client_code", "client_code": "client_code",
}
WORK_ORDER_GROUP_FIELDS = {
    "sector": "sector", "billing_status": "unified_billing_status",
    "unified_billing_status": "unified_billing_status", "status": "unified_billing_status",
    "client": "client_name", "client_name": "client_name", "owner": "owner",
    "type_of_work": "type_of_work",
}


def _text_matches(value: Optional[str], query: str, partial: bool = False) -> bool:
    """Case-insensitive match; partial lets "Negotiations" match "F. Negotiations"."""
    if not value:
        return False
    value, query = value.lower(), query.strip().lower()
    return query in value if partial else value == query


def _group_records(records: List[Dict[str, Any]], field: str) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        grouped.setdefault(r.get(field) or "Unknown", []).append(r)
    return grouped


def summarize_deal_values(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Value aggregates for a set of normalized deals.
    Weighted pipeline only covers Open deals in stages A-F (BUILD_BRIEF 2.6) and
    always reports how many of those used the stage-probability fallback.
    """
    values = [float(r['masked_deal_value']) for r in records if r.get('masked_deal_value') is not None]
    total_value = sum(values)
    weighted_scope = [
        r for r in records
        if r.get('deal_status') == 'Open' and r.get('deal_stage') in STAGE_PROBABILITY
    ]
    fallback_count = sum(1 for r in weighted_scope if r.get('used_probability_fallback'))
    return {
        "count": len(records),
        "total_masked_deal_value": total_value,
        # Deal values are heavily right-skewed: median is the "typical deal size"
        "median_deal_value": statistics.median(values) if values else None,
        "mean_deal_value": statistics.mean(values) if values else None,
        "value_disclosure": calculate_materiality_disclosure(
            len(records), len(records) - len(values), total_value, "masked_deal_value"
        ),
        "total_weighted_pipeline_value": sum(
            float(r.get('masked_deal_value') or 0) * float(r.get('stage_probability') or 0)
            for r in weighted_scope
        ),
        "weighted_pipeline_deal_count": len(weighted_scope),
        "probability_fallback": {
            "count": fallback_count,
            "fraction": fallback_count / len(weighted_scope) if weighted_scope else 0.0,
        },
    }


def summarize_work_order_values(records: List[Dict[str, Any]], excluded: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Billing aggregates for a set of normalized work orders. `excluded` holds the
    in-scope rows dropped for invalid (negative) amounts, for the materiality disclosure.
    """
    total_billed = sum(float(r.get('amount_to_billed') or 0) for r in records)
    total_receivable = sum(float(r.get('amount_receivable') or 0) for r in records)
    matched = len(records) + len(excluded)
    return {
        "count": len(records),
        "total_amount_to_be_billed": total_billed,
        "total_amount_receivable": total_receivable,
        "excluded_invalid_rows": len(excluded),
        "amount_to_be_billed_disclosure": calculate_materiality_disclosure(
            matched, len(excluded), total_billed, "amount_to_billed"
        ),
        "amount_receivable_disclosure": calculate_materiality_disclosure(
            matched, len(excluded), total_receivable, "amount_receivable"
        ),
    }


def apply_deals_filters(records: List[Dict[str, Any]], filters: DealFilters) -> List[Dict[str, Any]]:
    """Apply filters to deals records."""
    filtered = records
    
    if filters.sector:
        filtered = [r for r in filtered if r.get('sector') and filters.sector.lower() in r.get('sector').lower()]
    
    if filters.stage:
        filtered = [r for r in filtered if _text_matches(r.get('deal_stage'), filters.stage, partial=True)]
    
    if filters.status:
        if filters.status.strip().lower() not in ALL_STATUSES:
            filtered = [r for r in filtered if _text_matches(r.get('deal_status'), filters.status)]
    else:
        # Default to Open deals if not specified
        filtered = [r for r in filtered if r.get('deal_status') == 'Open']
    
    if filters.owner:
        filtered = [r for r in filtered if _text_matches(r.get('owner'), filters.owner, partial=True)]
    
    if filters.date_from:
        date_from = datetime.strptime(filters.date_from, '%Y-%m-%d').date()
        filtered = [r for r in filtered if r.get('tentative_close_date')]
        filtered = [r for r in filtered if 
                   datetime.strptime(r['tentative_close_date'], '%Y-%m-%d').date() >= date_from]
    
    if filters.date_to:
        date_to = datetime.strptime(filters.date_to, '%Y-%m-%d').date()
        filtered = [r for r in filtered if r.get('tentative_close_date')]
        filtered = [r for r in filtered if 
                   datetime.strptime(r['tentative_close_date'], '%Y-%m-%d').date() <= date_to]
    
    return filtered


def summarize_overdue_open_deals(records: List[Dict[str, Any]], filters: DealFilters) -> Dict[str, Any]:
    """
    Open deals (matching the non-date filters) whose tentative close date has
    already passed. These sit outside any current/future period, so an
    in-period pipeline can look empty while slipped deals are still open.
    """
    scope = DealFilters(sector=filters.sector, stage=filters.stage, status="Open", owner=filters.owner)
    today = date.today()
    overdue = [
        r for r in apply_deals_filters(records, scope)
        if r.get('tentative_close_date')
        and datetime.strptime(r['tentative_close_date'], '%Y-%m-%d').date() < today
    ]
    return {
        "as_of": today.isoformat(),
        "count": len(overdue),
        "total_masked_deal_value": sum(float(r.get('masked_deal_value') or 0) for r in overdue),
        "latest_tentative_close_date": max((r['tentative_close_date'] for r in overdue), default=None),
    }


def apply_work_orders_filters(records: List[Dict[str, Any]], filters: WorkOrderFilters) -> List[Dict[str, Any]]:
    """Apply filters to work orders records."""
    filtered = records
    
    if filters.sector:
        filtered = [r for r in filtered if r.get('sector') and filters.sector.lower() in r.get('sector').lower()]
    
    if filters.billing_status:
        filtered = [r for r in filtered if _text_matches(r.get('unified_billing_status'), filters.billing_status)]
    
    if filters.client:
        filtered = [r for r in filtered if 
                    (r.get('client_name') and filters.client.lower() in r.get('client_name').lower()) or 
                    (r.get('client_code') and filters.client.lower() in r.get('client_code').lower())]
    
    if filters.date_from:
        date_from = datetime.strptime(filters.date_from, '%Y-%m-%d').date()
        filtered = [r for r in filtered if r.get('invoice_date')]
        filtered = [r for r in filtered if 
                   datetime.strptime(r['invoice_date'], '%Y-%m-%d').date() >= date_from]
    
    if filters.date_to:
        date_to = datetime.strptime(filters.date_to, '%Y-%m-%d').date()
        filtered = [r for r in filtered if r.get('invoice_date')]
        filtered = [r for r in filtered if 
                   datetime.strptime(r['invoice_date'], '%Y-%m-%d').date() <= date_to]
    
    return filtered


def get_deals(
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    owner: Optional[str] = None
) -> ToolResult:
    """
    Get deals data with optional filters.
    
    Args:
        sector: Filter by sector
        stage: Filter by deal stage
        status: Filter by deal status (defaults to "Open" if not specified)
        date_from: Filter by tentative close date from (ISO format YYYY-MM-DD)
        date_to: Filter by tentative close date to (ISO format YYYY-MM-DD)
        owner: Filter by owner
    
    Returns:
        ToolResult with normalized deals records and quality report
    """
    try:
        # Create filters object
        filters = DealFilters(
            sector=sector,
            stage=stage,
            status=status,
            date_from=date_from,
            date_to=date_to,
            owner=owner
        )
        
        # Get raw data from Monday.com MCP (or the Excel fallback)
        raw_data = sync_get_deals_data()
        
        if not raw_data:
            return ToolResult(
                success=True,
                data=[],
                quality_report=DataQualityReport(
                    cleaned_records=[],
                    excluded_count=0,
                    excluded_fraction=0.0,
                    anomalies=[],
                    total_rows=0
                ),
                metadata={"message": "No deals data available from the data source"}
            )
        
        # Normalize data
        quality_report = normalize_deals_batch(raw_data)
        
        # Apply filters
        filtered_records = apply_deals_filters(quality_report.cleaned_records, filters)
        
        # Drop near-null columns to reduce context
        filtered_records = drop_null_columns(filtered_records)
        
        return ToolResult(
            success=True,
            data=filtered_records,
            quality_report=quality_report,
            metadata={
                "total_raw_records": len(raw_data),
                "filtered_records_count": len(filtered_records),
                "aggregations": summarize_deal_values(filtered_records),
                "filters_applied": asdict(filters),
                "overdue_open_deals": summarize_overdue_open_deals(quality_report.cleaned_records, filters),
                "currency": "INR",
                "materiality_threshold": MATERIALITY_THRESHOLD
            }
        )
        
    except Exception as e:
        return ToolResult(
            success=False,
            data=[],
            error=str(e)
        )


def get_work_orders(
    sector: Optional[str] = None,
    billing_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    client: Optional[str] = None
) -> ToolResult:
    """
    Get work orders data with optional filters.
    
    Args:
        sector: Filter by sector
        billing_status: Filter by unified billing status
        date_from: Filter by invoice date from (ISO format YYYY-MM-DD)
        date_to: Filter by invoice date to (ISO format YYYY-MM-DD)
        client: Filter by client name or code
    
    Returns:
        ToolResult with normalized work orders records and quality report
    """
    try:
        # Create filters object
        filters = WorkOrderFilters(
            sector=sector,
            billing_status=billing_status,
            date_from=date_from,
            date_to=date_to,
            client=client
        )
        
        # Get raw data from Monday.com MCP (or the Excel fallback)
        raw_data = sync_get_work_orders_data()
        
        if not raw_data:
            return ToolResult(
                success=True,
                data=[],
                quality_report=DataQualityReport(
                    cleaned_records=[],
                    excluded_count=0,
                    excluded_fraction=0.0,
                    anomalies=[],
                    total_rows=0
                ),
                metadata={"message": "No work orders data available from the data source"}
            )
        
        # Normalize data
        quality_report = normalize_work_orders_batch(raw_data)
        
        # Apply filters
        filtered_records = apply_work_orders_filters(quality_report.cleaned_records, filters)
        
        # Drop near-null columns to reduce context
        filtered_records = drop_null_columns(filtered_records)
        
        excluded_in_scope = apply_work_orders_filters(quality_report.excluded_records, filters)
        
        return ToolResult(
            success=True,
            data=filtered_records,
            quality_report=quality_report,
            metadata={
                "total_raw_records": len(raw_data),
                "filtered_records_count": len(filtered_records),
                "aggregations": summarize_work_order_values(filtered_records, excluded_in_scope),
                "filters_applied": asdict(filters),
                "currency": "INR",
                "materiality_threshold": MATERIALITY_THRESHOLD
            }
        )
        
    except Exception as e:
        return ToolResult(
            success=False,
            data=[],
            error=str(e)
        )



def get_deal_metrics(
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    owner: Optional[str] = None,
    group_by: Optional[str] = None
) -> ToolResult:
    """
    Get aggregated deal metrics (sums, counts) with optional filtering and grouping.
    """
    group_field = None
    if group_by:
        group_field = DEAL_GROUP_FIELDS.get(group_by.strip().lower())
        if not group_field:
            return ToolResult(
                success=False,
                data={},
                error=f"Invalid group_by '{group_by}'. Use one of: {', '.join(sorted(DEAL_GROUP_FIELDS))}"
            )
    
    # Re-use get_deals logic but only return metrics
    result = get_deals(sector, stage, status, date_from, date_to, owner)
    if not result.success:
        return result
        
    records = result.data
    
    if not group_field:
        metrics = summarize_deal_values(records)
    else:
        metrics = {
            "overall": summarize_deal_values(records),
            "groups": {k: summarize_deal_values(v) for k, v in _group_records(records, group_field).items()},
        }
        
    return ToolResult(
        success=True,
        data={"metrics": metrics},
        quality_report=result.quality_report,
        metadata={
            "filters_applied": result.metadata.get("filters_applied"),
            "group_by_field": group_field,
            "overdue_open_deals": result.metadata.get("overdue_open_deals"),
            "currency": "INR"
        }
    )

def get_work_order_metrics(
    sector: Optional[str] = None,
    billing_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    client: Optional[str] = None,
    group_by: Optional[str] = None
) -> ToolResult:
    """
    Get aggregated work order metrics (sums, counts) with optional filtering and grouping.
    """
    group_field = None
    if group_by:
        group_field = WORK_ORDER_GROUP_FIELDS.get(group_by.strip().lower())
        if not group_field:
            return ToolResult(
                success=False,
                data={},
                error=f"Invalid group_by '{group_by}'. Use one of: {', '.join(sorted(WORK_ORDER_GROUP_FIELDS))}"
            )
    
    result = get_work_orders(sector, billing_status, date_from, date_to, client)
    if not result.success:
        return result
        
    records = result.data
    excluded = apply_work_orders_filters(
        result.quality_report.excluded_records, WorkOrderFilters(**result.metadata["filters_applied"])
    )
    
    if not group_field:
        metrics = summarize_work_order_values(records, excluded)
    else:
        excluded_groups = _group_records(excluded, group_field)
        metrics = {
            "overall": summarize_work_order_values(records, excluded),
            "groups": {
                k: summarize_work_order_values(v, excluded_groups.get(k, []))
                for k, v in _group_records(records, group_field).items()
            },
        }
        
    return ToolResult(
        success=True,
        data={"metrics": metrics},
        quality_report=result.quality_report,
        metadata={
            "filters_applied": result.metadata.get("filters_applied"),
            "group_by_field": group_field,
            "currency": "INR"
        }
    )


def get_data_quality_summary(board: str) -> ToolResult:
    """
    Get on-demand full anomaly/sparsity report for a board.
    
    Args:
        board: Either "deals" or "work_orders"
    
    Returns:
        ToolResult with detailed data quality report
    """
    try:
        if board == "deals":
            raw_data = sync_get_deals_data()
            quality_report = normalize_deals_batch(raw_data)
        elif board == "work_orders":
            raw_data = sync_get_work_orders_data()
            quality_report = normalize_work_orders_batch(raw_data)
        else:
            return ToolResult(
                success=False,
                data={},
                error=f"Invalid board name: {board}. Use 'deals' or 'work_orders'"
            )
        
        # Enhance quality report with additional statistics
        summary = {
            "total_rows": quality_report.total_rows,
            "cleaned_rows": len(quality_report.cleaned_records),
            "excluded_count": quality_report.excluded_count,
            "excluded_fraction": quality_report.excluded_fraction,
            "anomaly_count": len(quality_report.anomalies),
            "anomalies_by_type": {},
            "sample_anomalies": []
        }
        
        # Group anomalies by type
        for anomaly in quality_report.anomalies:
            anomaly_type = anomaly.type.value if hasattr(anomaly.type, 'value') else str(anomaly.type)
            if anomaly_type not in summary["anomalies_by_type"]:
                summary["anomalies_by_type"][anomaly_type] = 0
            summary["anomalies_by_type"][anomaly_type] += 1
        
        # Add sample anomalies (first 10)
        summary["sample_anomalies"] = [
            {
                "row_id": a.row_id,
                "type": a.type.value if hasattr(a.type, 'value') else str(a.type),
                "detail": a.detail,
                "field": a.field
            }
            for a in quality_report.anomalies[:10]
        ]
        
        # Calculate column sparsity
        if quality_report.cleaned_records:
            column_stats = {}
            for record in quality_report.cleaned_records:
                for key, value in record.items():
                    if key not in column_stats:
                        column_stats[key] = {"total": 0, "null": 0}
                    column_stats[key]["total"] += 1
                    if value is None:
                        column_stats[key]["null"] += 1
            
            summary["column_sparsity"] = {
                col: {
                    "null_count": stats["null"],
                    "null_fraction": stats["null"] / stats["total"] if stats["total"] > 0 else 0
                }
                for col, stats in column_stats.items()
            }
        
        return ToolResult(
            success=True,
            data=summary,
            quality_report=quality_report
        )
        
    except Exception as e:
        return ToolResult(
            success=False,
            data={},
            error=str(e)
        )


def get_org_settings() -> ToolResult:
    """
    Get all organization settings.
    
    Returns:
        ToolResult with current org settings
    """
    try:
        settings = _active_org_settings.get().get_all_settings()
        return ToolResult(
            success=True,
            data=settings
        )
    except Exception as e:
        return ToolResult(
            success=False,
            data={},
            error=str(e)
        )


def set_org_setting(key: str, value: Any) -> ToolResult:
    """
    Set an organization setting.
    
    Args:
        key: Setting key (e.g., "fiscal_quarter_start_month")
        value: Setting value
    
    Returns:
        ToolResult confirming the setting was saved
    """
    try:
        _active_org_settings.get().set_setting(key, value)
        return ToolResult(
            success=True,
            data={"key": key, "value": value},
            metadata={"message": f"Setting '{key}' has been updated"}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            data={},
            error=str(e)
        )


def get_board_schemas() -> ToolResult:
    """
    Get column schemas for both boards.
    Useful for understanding available fields and their types.
    
    Returns:
        ToolResult with board schemas
    """
    try:
        schemas = sync_get_board_schemas()
        return ToolResult(
            success=True,
            data=schemas
        )
    except Exception as e:
        return ToolResult(
            success=False,
            data={},
            error=str(e)
        )


# Tool definitions for the agent
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_deal_metrics",
            "description": "Get aggregated metrics for deals: count, total value, median/mean deal value, weighted pipeline (Open deals in stages A-F only) with probability-fallback disclosure, and value materiality disclosure. All values in INR. Supports filtering and optional grouping. Use this instead of get_deals when the user asks for sums, totals, counts or averages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "stage": {"type": ["string", "null"], "description": "Filter by deal stage; partial, case-insensitive match (e.g. 'Negotiations' matches 'F. Negotiations')"},
                    "status": {"type": ["string", "null"], "description": "Filter by deal status: Open, Won, Dead, On Hold, or 'All' for every status (defaults to 'Open' if not specified)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by tentative close date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by tentative close date to (ISO format YYYY-MM-DD)"},
                    "owner": {"type": ["string", "null"], "description": "Filter by owner code (e.g. 'OWNER_001')"},
                    "group_by": {"type": ["string", "null"], "description": "Group results by one of: sector, stage, status, owner, client"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_work_order_metrics",
            "description": "Get aggregated metrics for work orders: count, total amount to be billed, total receivable, with materiality disclosure for rows excluded for invalid (negative) amounts. All values in INR. Supports filtering and grouping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "billing_status": {"type": ["string", "null"], "description": "Filter by unified billing status (exact value, case-insensitive; see domain values)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by last invoice date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by last invoice date to (ISO format YYYY-MM-DD)"},
                    "client": {"type": ["string", "null"], "description": "Filter by client name or code"},
                    "group_by": {"type": ["string", "null"], "description": "Group results by one of: sector, billing_status, client, owner, type_of_work"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_deals",
            "description": "Get deals data with optional filters. Defaults to Open deals unless status is specified. Returns normalized records with data quality report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "stage": {"type": ["string", "null"], "description": "Filter by deal stage; partial, case-insensitive match (e.g. 'Negotiations' matches 'F. Negotiations')"},
                    "status": {"type": ["string", "null"], "description": "Filter by deal status: Open, Won, Dead, On Hold, or 'All' for every status (defaults to 'Open' if not specified)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by tentative close date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by tentative close date to (ISO format YYYY-MM-DD)"},
                    "owner": {"type": ["string", "null"], "description": "Filter by owner code (e.g. 'OWNER_001')"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_work_orders",
            "description": "Get work orders data with optional filters. Uses unified billing status logic. Returns normalized records with data quality report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "billing_status": {"type": ["string", "null"], "description": "Filter by unified billing status (exact value, case-insensitive; see domain values)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by last invoice date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by last invoice date to (ISO format YYYY-MM-DD)"},
                    "client": {"type": ["string", "null"], "description": "Filter by client name or code"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_quality_summary",
            "description": "Get on-demand full anomaly/sparsity report for a board. Use when user asks about data quality.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board": {"type": "string", "enum": ["deals", "work_orders"], "description": "Board to analyze"}
                },
                "required": ["board"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_org_settings",
            "description": "Get all organization settings including fiscal quarter convention.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_org_setting",
            "description": "Set an organization setting (e.g., fiscal quarter start month).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Setting key"},
                    "value": {"type": "string", "description": "Setting value"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_board_schemas",
            "description": "Get column schemas for both boards to understand available fields and their types.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

# Tool mapping for the agent to call functions
TOOL_FUNCTIONS = {
    "get_deals": get_deals,
    "get_deal_metrics": get_deal_metrics,
    "get_work_orders": get_work_orders,
    "get_work_order_metrics": get_work_order_metrics,
    "get_data_quality_summary": get_data_quality_summary,
    "get_org_settings": get_org_settings,
    "set_org_setting": set_org_setting,
    "get_board_schemas": get_board_schemas
}
