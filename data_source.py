"""
Data Source Router for Monday.com Business Intelligence Agent

Decides where raw board rows come from, so the rest of the agent stays
source-agnostic. Controlled by the DATA_SOURCE environment variable:
- "monday": Monday.com MCP only (errors surface to the agent)
- "excel":  bundled Excel snapshots in Data/ (free, no Monday.com account)
- "auto":   Monday.com MCP when MONDAY_API_TOKEN is set, falling back to the
            Excel snapshots if the MCP read fails (default)

MCP reads are cached for DATA_CACHE_SECONDS (default 120, 0 disables) so a
question that calls several tools doesn't re-download the boards each time.

Both sources return rows keyed by column title, which the normalizer accepts.
"""

import copy
import os
import time
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Callable, Dict, List, Tuple

import pandas as pd
from dotenv import load_dotenv

from mcp_integration import sync_get_deals_data, sync_get_work_orders_data, sync_get_board_schemas
from setup_boards import load_and_clean_deals_data, load_and_clean_work_orders_data

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data")
DEALS_FILE = os.path.join(DATA_DIR, "Deal funnel Data.xlsx")
WORK_ORDERS_FILE = os.path.join(DATA_DIR, "Work_Order_Tracker Data.xlsx")

SOURCE_MONDAY = "monday_mcp"
SOURCE_EXCEL = "excel_snapshot"

# After an MCP failure in auto mode, skip retrying for this long so every tool
# call doesn't pay a connection timeout before falling back.
MCP_RETRY_COOLDOWN_SECONDS = 600

_state: Dict[str, Any] = {"last_source": None, "fallback_reason": None, "mcp_failed_at": None}

# Board key -> (fetched_at, MCP result)
_mcp_cache: Dict[str, Tuple[float, Any]] = {}


def get_mode() -> str:
    """Return the configured mode: 'auto', 'monday' or 'excel'."""
    mode = os.environ.get("DATA_SOURCE", "auto").strip().lower()
    return mode if mode in ("auto", "monday", "excel") else "auto"


def get_cache_seconds() -> int:
    """How long MCP reads are reused; 0 disables caching."""
    try:
        return max(0, int(os.environ.get("DATA_CACHE_SECONDS", "120")))
    except ValueError:
        return 120


def clear_cache():
    """Drop cached MCP reads so the next read fetches fresh board data."""
    _mcp_cache.clear()


def get_status() -> Dict[str, Any]:
    """Describe the configured mode and the source actually used last."""
    return {
        "mode": get_mode(),
        "last_source": _state["last_source"],
        "fallback_reason": _state["fallback_reason"],
        "cache_seconds": get_cache_seconds(),
    }


def _cached_mcp_read(key: str, mcp_reader: Callable[[], Any]) -> Any:
    ttl = get_cache_seconds()
    cached = _mcp_cache.get(key)
    if ttl and cached and time.time() - cached[0] < ttl:
        return copy.deepcopy(cached[1])
    result = mcp_reader()
    if ttl:
        _mcp_cache[key] = (time.time(), result)
    # Copy so callers can't mutate the cached read.
    return copy.deepcopy(result)


def _row_to_record(row: pd.Series) -> Dict[str, Any]:
    """Mirror how setup_boards imports rows: drop blanks, stringify values."""
    record = {}
    for col, value in row.items():
        if pd.isna(value) or str(value).strip() == "":
            continue
        if isinstance(value, (datetime, date)):
            # Monday date columns surface as YYYY-MM-DD; match that format.
            record[str(col)] = value.strftime("%Y-%m-%d")
        else:
            record[str(col)] = str(value)
    return record


@lru_cache(maxsize=None)
def _load_excel_records(board: str) -> tuple:
    if board == "deals":
        df = load_and_clean_deals_data(DEALS_FILE)
    else:
        df = load_and_clean_work_orders_data(WORK_ORDERS_FILE)
    return tuple(_row_to_record(row) for _, row in df.iterrows())


def _excel_rows(board: str) -> List[Dict[str, Any]]:
    # Copy so callers can't mutate the cached snapshot.
    return [dict(record) for record in _load_excel_records(board)]


def _excel_schemas() -> Dict[str, List[Dict[str, Any]]]:
    schemas = {}
    for board in ("deals", "work_orders"):
        titles = []
        for record in _load_excel_records(board):
            titles.extend(title for title in record if title not in titles)
        schemas[board] = [{"id": title, "title": title, "type": "text"} for title in titles]
    return schemas


def _read(key: str, mcp_reader: Callable[[], Any], excel_reader: Callable[[], Any]) -> Any:
    mode = get_mode()

    if mode == "excel":
        _state.update(last_source=SOURCE_EXCEL, fallback_reason=None)
        return excel_reader()

    if mode == "auto":
        if not os.environ.get("MONDAY_API_TOKEN"):
            _state.update(last_source=SOURCE_EXCEL, fallback_reason="MONDAY_API_TOKEN not set")
            return excel_reader()
        failed_at = _state["mcp_failed_at"]
        if failed_at and time.time() - failed_at < MCP_RETRY_COOLDOWN_SECONDS:
            _state["last_source"] = SOURCE_EXCEL
            return excel_reader()

    try:
        result = _cached_mcp_read(key, mcp_reader)
    except Exception as e:
        if mode == "monday":
            raise
        print(f"Monday.com MCP read failed, using Excel snapshot: {e}")
        _state.update(
            last_source=SOURCE_EXCEL,
            fallback_reason=f"Monday.com MCP unavailable: {e}",
            mcp_failed_at=time.time(),
        )
        return excel_reader()

    _state.update(last_source=SOURCE_MONDAY, fallback_reason=None, mcp_failed_at=None)
    return result


def get_deals_data() -> List[Dict[str, Any]]:
    """Raw Deals rows from the active source."""
    return _read("deals", sync_get_deals_data, lambda: _excel_rows("deals"))


def get_work_orders_data() -> List[Dict[str, Any]]:
    """Raw Work Orders rows from the active source."""
    return _read("work_orders", sync_get_work_orders_data, lambda: _excel_rows("work_orders"))


def get_board_schemas() -> Dict[str, List[Dict[str, Any]]]:
    """Column schemas for both boards from the active source."""
    def mcp_schemas():
        # sync_get_board_schemas returns empty lists instead of raising when it
        # can't connect; treat that as a failure so auto mode can fall back.
        schemas = sync_get_board_schemas()
        if not any(schemas.values()):
            raise RuntimeError("Unable to connect to the Monday MCP server")
        return schemas

    return _read("schemas", mcp_schemas, _excel_schemas)
