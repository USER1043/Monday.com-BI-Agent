"""
Purpose-Built Tools for Monday.com Business Intelligence Agent

Wrappers around MCP + normalizer that expose business-logic tools to Claude.
Per BUILD_BRIEF.md Section 4:
- get_deals(filters) — sector, stage, status, date range, owner
- get_work_orders(filters) — sector, unified billing status, date range, client
- get_data_quality_summary(board) — on-demand full anomaly/sparsity report
- get_org_settings() / set_org_setting(key, value) — persistence for fiscal-quarter convention
"""

import json
import os
from typing import Dict, List, Any, Optional, Union
from datetime import datetime, date
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from normalization import (
    normalize_deals_batch, normalize_work_orders_batch, 
    DataQualityReport, AnomalyType, drop_null_columns,
    calculate_materiality_disclosure, MATERIALITY_THRESHOLD
)
from mcp_integration import sync_get_deals_data, sync_get_work_orders_data, sync_get_board_schemas


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
    """Simple persistence for organization-level settings like fiscal quarter convention."""
    
    def __init__(self, settings_file: str = ".org_settings.json"):
        self.settings_file = settings_file
        self._settings = self._load_settings()
    
    def _load_settings(self) -> Dict[str, Any]:
        """Load settings from file."""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}
    
    def _save_settings(self):
        """Save settings to file."""
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


# Global org settings instance
org_settings = OrgSettings()


def apply_deals_filters(records: List[Dict[str, Any]], filters: DealFilters) -> List[Dict[str, Any]]:
    """Apply filters to deals records."""
    filtered = records
    
    if filters.sector:
        filtered = [r for r in filtered if r.get('sector') == filters.sector]
    
    if filters.stage:
        filtered = [r for r in filtered if r.get('deal_stage') == filters.stage]
    
    if filters.status:
        filtered = [r for r in filtered if r.get('deal_status') == filters.status]
    else:
        # Default to Open deals if not specified
        filtered = [r for r in filtered if r.get('deal_status') == 'Open']
    
    if filters.owner:
        filtered = [r for r in filtered if r.get('owner') == filters.owner]
    
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


def apply_work_orders_filters(records: List[Dict[str, Any]], filters: WorkOrderFilters) -> List[Dict[str, Any]]:
    """Apply filters to work orders records."""
    filtered = records
    
    if filters.sector:
        filtered = [r for r in filtered if r.get('sector') == filters.sector]
    
    if filters.billing_status:
        filtered = [r for r in filtered if r.get('unified_billing_status') == filters.billing_status]
    
    if filters.client:
        filtered = [r for r in filtered if r.get('client_name') == filters.client or 
                   r.get('client_code') == filters.client]
    
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
        
        # Get raw data from MCP
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
                metadata={"message": "No deals data available from MCP"}
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
                "filtered_records": len(filtered_records),
                "filters_applied": asdict(filters),
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
        
        # Get raw data from MCP
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
                metadata={"message": "No work orders data available from MCP"}
            )
        
        # Normalize data
        quality_report = normalize_work_orders_batch(raw_data)
        
        # Apply filters
        filtered_records = apply_work_orders_filters(quality_report.cleaned_records, filters)
        
        # Drop near-null columns to reduce context
        filtered_records = drop_null_columns(filtered_records)
        
        return ToolResult(
            success=True,
            data=filtered_records,
            quality_report=quality_report,
            metadata={
                "total_raw_records": len(raw_data),
                "filtered_records": len(filtered_records),
                "filters_applied": asdict(filters),
                "materiality_threshold": MATERIALITY_THRESHOLD
            }
        )
        
    except Exception as e:
        return ToolResult(
            success=False,
            data=[],
            error=str(e)
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
        settings = org_settings.get_all_settings()
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
        org_settings.set_setting(key, value)
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
            "name": "get_deals",
            "description": "Get deals data with optional filters. Defaults to Open deals unless status is specified. Returns normalized records with data quality report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": ["string", "null"], "description": "Filter by sector"},
                    "stage": {"type": ["string", "null"], "description": "Filter by deal stage"},
                    "status": {"type": ["string", "null"], "description": "Filter by deal status (defaults to 'Open' if not specified)"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by tentative close date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by tentative close date to (ISO format YYYY-MM-DD)"},
                    "owner": {"type": ["string", "null"], "description": "Filter by owner"}
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
                    "billing_status": {"type": ["string", "null"], "description": "Filter by unified billing status"},
                    "date_from": {"type": ["string", "null"], "description": "Filter by invoice date from (ISO format YYYY-MM-DD)"},
                    "date_to": {"type": ["string", "null"], "description": "Filter by invoice date to (ISO format YYYY-MM-DD)"},
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
    "get_work_orders": get_work_orders,
    "get_data_quality_summary": get_data_quality_summary,
    "get_org_settings": get_org_settings,
    "set_org_setting": set_org_setting,
    "get_board_schemas": get_board_schemas
}
