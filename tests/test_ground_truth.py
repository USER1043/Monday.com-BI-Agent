"""
Ground-truth checks: every tool figure is compared against the same figure
computed independently from the raw Excel files with pandas, without going
through the normalization layer.
"""

import os

import pandas as pd
import pytest

import tools
from conftest import ROOT

STAGE_PROBABILITY = {
    "A. Lead Generated": 0.10, "B. Sales Qualified Leads": 0.20, "C. Demo Done": 0.35,
    "D. Feasibility": 0.45, "E. Proposal/Commercials Sent": 0.60, "F. Negotiations": 0.80,
}
TO_BE_BILLED = "Amount to be billed in Rs. (Exl. of GST) (Masked)"
RECEIVABLE = "Amount Receivable (Masked)"


def _load_deals() -> pd.DataFrame:
    deals = pd.read_excel(os.path.join(ROOT, "Data", "Deal funnel Data.xlsx"))
    deals = deals[deals["Deal Status"] != "Deal Status"].drop_duplicates()
    deals["Deal Status"] = deals["Deal Status"].fillna("Unknown")
    deals["close"] = pd.to_datetime(deals["Tentative Close Date"], errors="coerce")
    return deals


def _unified_billing_status(row) -> str:
    for column in ("Invoice Status", "WO Status (billed)", "Billing Status"):
        value = row[column]
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "unknown"


def _load_work_orders() -> pd.DataFrame:
    work_orders = pd.read_excel(os.path.join(ROOT, "Data", "Work_Order_Tracker Data.xlsx"), header=1)
    # Negative amounts are invalid and excluded from clean aggregates
    work_orders = work_orders[~((work_orders[TO_BE_BILLED] < 0) | (work_orders[RECEIVABLE] < 0))].copy()
    work_orders["unified"] = work_orders.apply(_unified_billing_status, axis=1)
    work_orders["invoiced"] = pd.to_datetime(work_orders["Last invoice date"], errors="coerce")
    return work_orders


DEALS = _load_deals()
OPEN = DEALS[DEALS["Deal Status"] == "Open"]
WORK_ORDERS = _load_work_orders()


def _expected_deal_metrics(df: pd.DataFrame) -> dict:
    in_weighted_scope = df[(df["Deal Status"] == "Open") & df["Deal Stage"].isin(STAGE_PROBABILITY)]
    values = df["Masked Deal value"].dropna()
    return {
        "count": len(df),
        "total_masked_deal_value": values.sum(),
        "total_weighted_pipeline_value": (
            in_weighted_scope["Masked Deal value"].fillna(0) * in_weighted_scope["Deal Stage"].map(STAGE_PROBABILITY)
        ).sum(),
        "median_deal_value": values.median() if len(values) else None,
    }


def _assert_metrics(actual: dict, expected: dict):
    for key, want in expected.items():
        got = actual[key]
        if want is None:
            assert got is None, key
        else:
            assert got == pytest.approx(want, rel=1e-6, abs=0.01), key


DEAL_CASES = [
    ("open by default", {}, OPEN),
    ("won", {"status": "won"}, DEALS[DEALS["Deal Status"] == "Won"]),
    ("dead", {"status": "dead"}, DEALS[DEALS["Deal Status"] == "Dead"]),
    ("on hold", {"status": "on hold"}, DEALS[DEALS["Deal Status"] == "On Hold"]),
    ("all statuses", {"status": "All"}, DEALS),
    ("stage partial match", {"stage": "Negotiations"}, OPEN[OPEN["Deal Stage"] == "F. Negotiations"]),
    ("sector case-insensitive", {"sector": "mining"},
     OPEN[OPEN["Sector/service"].fillna("").str.lower().str.contains("mining")]),
    ("owner case-insensitive", {"owner": "owner_001"}, OPEN[OPEN["Owner code"] == "OWNER_001"]),
    ("tentative close in Q1 2026", {"date_from": "2026-01-01", "date_to": "2026-03-31"},
     OPEN[(OPEN["close"] >= "2026-01-01") & (OPEN["close"] <= "2026-03-31")]),
    ("won in mining", {"status": "Won", "sector": "Mining"},
     DEALS[(DEALS["Deal Status"] == "Won") & (DEALS["Sector/service"] == "Mining")]),
]


@pytest.mark.parametrize("kwargs,expected_rows", [c[1:] for c in DEAL_CASES], ids=[c[0] for c in DEAL_CASES])
def test_deal_metrics_match_raw_data(kwargs, expected_rows):
    result = tools.get_deal_metrics(**kwargs)
    assert result.success, result.error
    _assert_metrics(result.data["metrics"], _expected_deal_metrics(expected_rows))


@pytest.mark.parametrize("group_by,status,column", [
    ("sector", None, "Sector/service"),
    ("stage", "All", "Deal Stage"),
    ("owner", None, "Owner code"),
])
def test_deal_metrics_grouping(group_by, status, column):
    result = tools.get_deal_metrics(group_by=group_by, status=status)
    assert result.success, result.error
    rows = DEALS if status == "All" else OPEN
    groups = result.data["metrics"]["groups"]
    expected_groups = dict(tuple(rows.groupby(rows[column].fillna("Unknown"))))
    assert set(groups) == set(expected_groups)
    for name, group_rows in expected_groups.items():
        _assert_metrics(groups[name], _expected_deal_metrics(group_rows))


def test_weighted_pipeline_discloses_fallback():
    metrics = tools.get_deal_metrics().data["metrics"]
    in_scope = OPEN[OPEN["Deal Stage"].isin(STAGE_PROBABILITY)]
    assert metrics["weighted_pipeline_deal_count"] == len(in_scope)
    # Closure Probability is High/Medium/Low text in the source, so every deal falls back
    assert metrics["probability_fallback"]["count"] == len(in_scope)


def _expected_work_order_metrics(df: pd.DataFrame) -> dict:
    return {
        "count": len(df),
        "total_amount_to_be_billed": df[TO_BE_BILLED].fillna(0).sum(),
        "total_amount_receivable": df[RECEIVABLE].fillna(0).sum(),
    }


WORK_ORDER_CASES = [
    ("all", {}, WORK_ORDERS),
    ("sector", {"sector": "Mining"}, WORK_ORDERS[WORK_ORDERS["Sector"] == "Mining"]),
    ("billing status case-insensitive", {"billing_status": "fully billed"},
     WORK_ORDERS[WORK_ORDERS["unified"] == "fully billed"]),
    ("billing status typo casing", {"billing_status": "Not billed yet"},
     WORK_ORDERS[WORK_ORDERS["unified"] == "not billed yet"]),
    ("invoiced in Q3 2025", {"date_from": "2025-07-01", "date_to": "2025-09-30"},
     WORK_ORDERS[(WORK_ORDERS["invoiced"] >= "2025-07-01") & (WORK_ORDERS["invoiced"] <= "2025-09-30")]),
    ("client", {"client": "WOCOMPANY_002"}, WORK_ORDERS[WORK_ORDERS["Customer Name Code"] == "WOCOMPANY_002"]),
]


@pytest.mark.parametrize("kwargs,expected_rows", [c[1:] for c in WORK_ORDER_CASES],
                         ids=[c[0] for c in WORK_ORDER_CASES])
def test_work_order_metrics_match_raw_data(kwargs, expected_rows):
    result = tools.get_work_order_metrics(**kwargs)
    assert result.success, result.error
    _assert_metrics(result.data["metrics"], _expected_work_order_metrics(expected_rows))


def test_work_order_metrics_grouping():
    result = tools.get_work_order_metrics(group_by="sector")
    assert result.success, result.error
    groups = result.data["metrics"]["groups"]
    expected_groups = dict(tuple(WORK_ORDERS.groupby("Sector")))
    assert set(groups) == set(expected_groups)
    for name, group_rows in expected_groups.items():
        _assert_metrics(groups[name], _expected_work_order_metrics(group_rows))


@pytest.mark.parametrize("metrics_tool", [tools.get_deal_metrics, tools.get_work_order_metrics])
def test_invalid_group_by_is_rejected(metrics_tool):
    result = metrics_tool(group_by="banana")
    assert not result.success
    assert "Invalid group_by" in result.error
