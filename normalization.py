"""
Normalization Layer for Monday.com Business Intelligence Agent

Pure Python functions for data cleaning and normalization.
Fully unit-testable with no LLM/MCP involvement.

Per BUILD_BRIEF.md Section 2:
- Generic normalization (null handling, currency, dates, text, multi-select, embedded units)
- Unified Billing Status for Work Orders
- Deal Status vs Deal Stage handling for Deals
- Invalid value handling
- Materiality-threshold disclosure
- Weighted pipeline fallback probability
- Aggregation defaults
- Surrogate key generation
- Data-quality reporting
"""

import re
import hashlib
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum


# Constants
MATERIALITY_THRESHOLD = 0.10  # 10% threshold for materiality disclosure
NULL_SENTINELS = ["", "N/A", "TBD", "-", "#VALUE!", "NA", "n/a", "NULL", "null"]

STAGE_PROBABILITY = {
    "A. Lead Generated": 0.10,
    "B. Sales Qualified Leads": 0.20,
    "C. Demo Done": 0.35,
    "D. Feasibility": 0.45,
    "E. Proposal/Commercials Sent": 0.60,
    "F. Negotiations": 0.80,
}


class AnomalyType(Enum):
    """Types of data quality anomalies."""
    STATUS_CONTRADICTION = "status_contradiction"
    INVALID_VALUE = "invalid_value"
    STAGE_STATUS_MISMATCH = "stage_status_mismatch"
    MISSING_VALUE = "missing_value"
    PARSE_FAILURE = "parse_failure"


@dataclass
class Anomaly:
    """Represents a data quality anomaly."""
    row_id: str
    type: AnomalyType
    detail: str
    field: Optional[str] = None


@dataclass
class DataQualityReport:
    """Data quality report returned by normalization functions."""
    cleaned_records: List[Dict[str, Any]]
    excluded_count: int
    excluded_fraction: float
    anomalies: List[Anomaly]
    total_rows: int


def is_null_sentinel(value: Any) -> bool:
    """Check if a value is a null sentinel."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in NULL_SENTINELS
    return False


def normalize_null(value: Any) -> Optional[Any]:
    """Normalize null sentinels to None."""
    if is_null_sentinel(value):
        return None
    return value


def normalize_currency(value: Any) -> Optional[float]:
    """
    Normalize currency strings to float.
    Strips currency symbols, commas, and coerces to float.
    Returns None on failure (never silently zero).
    """
    if is_null_sentinel(value):
        return None
    
    try:
        if isinstance(value, (int, float)):
            return float(value)
        
        if isinstance(value, str):
            # Remove currency symbols and commas
            cleaned = re.sub(r'[^\d.-]', '', value)
            if not cleaned or cleaned == '.' or cleaned == '-':
                return None
            return float(cleaned)
        
        return None
    except (ValueError, TypeError):
        return None


def normalize_date(value: Any) -> Optional[str]:
    """
    Normalize dates to ISO 8601 format (YYYY-MM-DD).
    Returns None on failure.
    """
    if is_null_sentinel(value):
        return None
    
    try:
        if isinstance(value, str):
            # Try common date formats
            for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%Y/%m/%d', 
                       '%d-%m-%Y', '%m-%d-%Y', '%Y%m%d']:
                try:
                    dt = datetime.strptime(value.strip(), fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
            
            # Try Excel serial date format
            try:
                excel_date = float(value)
                if excel_date > 0:
                    # Excel epoch is 1900-01-01, but Excel incorrectly treats 1900 as a leap year
                    # For simplicity, we'll use a rough approximation
                    dt = datetime(1899, 12, 30) + datetime.timedelta(days=excel_date)
                    return dt.strftime('%Y-%m-%d')
            except ValueError:
                pass
        
        return None
    except (ValueError, TypeError):
        return None


def normalize_text(value: Any) -> Optional[str]:
    """
    Normalize text: trim whitespace, fix known casing typos.
    """
    if is_null_sentinel(value):
        return None
    
    if isinstance(value, str):
        text = value.strip()
        # Fix known casing typos
        text = text.replace("BIlled", "Billed")
        text = text.replace("billed", "Billed")  # Normalize to proper casing
        return text if text else None
    
    return str(value).strip() if value is not None else None


def normalize_multi_select(value: Any) -> Optional[str]:
    """
    Normalize multi-select fields by sorting/canonicalizing labels.
    Makes "A, B" and "B, A" group identically.
    """
    if is_null_sentinel(value):
        return None
    
    if isinstance(value, str):
        # Split by common delimiters
        labels = re.split(r'[,;]\s*', value.strip())
        # Clean and filter empty labels
        labels = [label.strip() for label in labels if label.strip()]
        # Sort for canonical order
        if labels:
            return ', '.join(sorted(labels))
    
    return None


def normalize_embedded_unit(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """
    Parse embedded-unit fields (e.g., "10 units", "5 kg") into quantity and unit.
    Returns (quantity_value, quantity_unit).
    Rows that don't parse cleanly return (None, None).
    """
    if is_null_sentinel(value):
        return None, None
    
    if isinstance(value, str):
        text = value.strip()
        
        # Try to extract number and unit
        # Pattern: optional number, then optional unit text
        match = re.match(r'^([-\d.]+)\s*(.*)$', text)
        if match:
            num_str, unit = match.groups()
            try:
                quantity = float(num_str)
                unit_text = unit.strip() if unit.strip() else None
                return quantity, unit_text
            except ValueError:
                pass
    
    return None, None


def unified_billing_status(invoice_status: Any, wo_status: Any, billing_status: Any) -> Tuple[str, List[Anomaly]]:
    """
    Compute unified billing status with precedence.
    Precedence: Invoice Status → WO Status (billed) → Billing Status → "Unknown"
    
    Also detects and flags contradictions when higher and lower precedence fields disagree.
    """
    anomalies = []
    
    # Normalize inputs
    invoice = normalize_text(invoice_status)
    wo = normalize_text(wo_status)
    billing = normalize_text(billing_status)
    
    # Determine unified status based on precedence
    if invoice is not None:
        unified = invoice
        # Check for contradictions with lower precedence fields
        if wo is not None and wo != invoice:
            anomalies.append(Anomaly(
                row_id="unknown",
                type=AnomalyType.STATUS_CONTRADICTION,
                detail=f"Invoice Status '{invoice}' conflicts with WO Status '{wo}'",
                field="billing_status"
            ))
        if billing is not None and billing != invoice:
            anomalies.append(Anomaly(
                row_id="unknown",
                type=AnomalyType.STATUS_CONTRADICTION,
                detail=f"Invoice Status '{invoice}' conflicts with Billing Status '{billing}'",
                field="billing_status"
            ))
    elif wo is not None:
        unified = wo
        if billing is not None and billing != wo:
            anomalies.append(Anomaly(
                row_id="unknown",
                type=AnomalyType.STATUS_CONTRADICTION,
                detail=f"WO Status '{wo}' conflicts with Billing Status '{billing}'",
                field="billing_status"
            ))
    elif billing is not None:
        unified = billing
    else:
        unified = "Unknown"
    
    return unified, anomalies


def validate_deal_status_stage(deal_status: Any, deal_stage: Any) -> Tuple[str, List[Anomaly]]:
    """
    Validate Deal Status vs Deal Stage.
    Deal Status is authoritative; Stage is never used to override.
    Flag disagreements as anomalies.
    """
    anomalies = []
    
    status = normalize_text(deal_status)
    stage = normalize_text(deal_stage)
    
    # Deal Status is authoritative
    authoritative_status = status if status is not None else "Unknown"
    
    # Flag disagreements
    if status is not None and stage is not None:
        # Simple heuristic: if status is Won/Dead but stage is early, flag it
        if status.lower() in ['won', 'dead'] and stage.lower().startswith('a.'):
            anomalies.append(Anomaly(
                row_id="unknown",
                type=AnomalyType.STAGE_STATUS_MISMATCH,
                detail=f"Deal Status '{status}' contradicts early Deal Stage '{stage}'",
                field="deal_stage"
            ))
    
    return authoritative_status, anomalies


def validate_financial_invariants(amount_to_billed: Any, amount_receivable: Any) -> List[Anomaly]:
    """
    Validate financial invariants.
    Negative amounts are invalid and should be flagged.
    Never auto-correct (no abs(), no clipping to zero).
    """
    anomalies = []
    
    billed = normalize_currency(amount_to_billed)
    receivable = normalize_currency(amount_receivable)
    
    if billed is not None and billed < 0:
        anomalies.append(Anomaly(
            row_id="unknown",
            type=AnomalyType.INVALID_VALUE,
            detail=f"Negative Amount to be billed: {billed}",
            field="amount_to_billed"
        ))
    
    if receivable is not None and receivable < 0:
        anomalies.append(Anomaly(
            row_id="unknown",
            type=AnomalyType.INVALID_VALUE,
            detail=f"Negative Amount Receivable: {receivable}",
            field="amount_receivable"
        ))
    
    return anomalies


def generate_deal_surrogate_key(client_code: Any, deal_name: Any, created_date: Any) -> str:
    """
    Generate surrogate key for deals: hash(client_code + deal_name + created_date).
    Deal Name alone is not unique.
    """
    key_string = f"{normalize_text(client_code) or ''}{normalize_text(deal_name) or ''}{normalize_date(created_date) or ''}"
    return hashlib.md5(key_string.encode()).hexdigest()


def get_stage_probability(deal_stage: Any, closure_probability: Any) -> Tuple[float, bool]:
    """
    Get probability for weighted pipeline calculation.
    Use Closure Probability directly when present; fall back to STAGE_PROBABILITY.
    
    Returns (probability, used_fallback).
    """
    # Try closure probability first
    closure_prob = normalize_currency(closure_probability)
    if closure_prob is not None and 0 <= closure_prob <= 1:
        return closure_prob, False
    
    # Fall back to stage-based probability
    stage = normalize_text(deal_stage)
    if stage and stage in STAGE_PROBABILITY:
        return STAGE_PROBABILITY[stage], True
    
    return 0.0, True


def calculate_materiality_disclosure(matched_rows: int, excluded_rows: int, 
                                     clean_value: float, field_name: str) -> Dict[str, Any]:
    """
    Calculate materiality disclosure for missing/malformed data.
    
    If excluded_fraction <= MATERIALITY_THRESHOLD: return clean figure + excluded_count
    If excluded_fraction > MATERIALITY_THRESHOLD: return confirmed figure + estimated upper bound
    """
    if matched_rows == 0:
        return {
            "value": clean_value,
            "excluded_count": excluded_rows,
            "excluded_fraction": 0.0,
            "disclosure_type": "no_data"
        }
    
    excluded_fraction = excluded_rows / matched_rows
    
    if excluded_fraction <= MATERIALITY_THRESHOLD:
        return {
            "value": clean_value,
            "excluded_count": excluded_rows,
            "excluded_fraction": excluded_fraction,
            "disclosure_type": "quiet_footnote"
        }
    else:
        # Calculate simple estimate: average of clean subset applied to excluded rows
        if matched_rows - excluded_rows > 0:
            average_value = clean_value / (matched_rows - excluded_rows)
            estimated_upper_bound = clean_value + (average_value * excluded_rows)
        else:
            estimated_upper_bound = clean_value
        
        return {
            "confirmed_value": clean_value,
            "estimated_upper_bound": estimated_upper_bound,
            "excluded_count": excluded_rows,
            "excluded_fraction": excluded_fraction,
            "disclosure_type": "estimate_range",
            "estimation_method": f"Average value of clean subset ({clean_value/(matched_rows - excluded_rows) if matched_rows - excluded_rows > 0 else 0:.2f}) applied to excluded rows"
        }


def normalize_deals_row(row: Dict[str, Any], row_id: str) -> Tuple[Dict[str, Any], List[Anomaly]]:
    """
    Normalize a single deals row.
    Returns (cleaned_row, anomalies).
    """
    cleaned = {}
    anomalies = []
    
    # Generate surrogate key
    client_code = row.get('Client Code', row.get('client_code', ''))
    deal_name = row.get('Deal Name', row.get('deal_name', ''))
    created_date = row.get('Created Date', row.get('created_date', ''))
    deal_id = generate_deal_surrogate_key(client_code, deal_name, created_date)
    cleaned['deal_id'] = deal_id
    
    # Normalize basic fields
    cleaned['deal_name'] = normalize_text(deal_name)
    cleaned['client_code'] = normalize_text(client_code)
    cleaned['sector'] = normalize_text(row.get('Sector', row.get('sector', '')))
    cleaned['owner'] = normalize_text(row.get('Owner', row.get('owner', '')))
    
    # Handle dates
    cleaned['created_date'] = normalize_date(created_date)
    cleaned['tentative_close_date'] = normalize_date(
        row.get('Tentative Close Date', row.get('tentative_close_date', ''))
    )
    
    # Handle Deal Status vs Stage
    deal_status = row.get('Deal Status', row.get('deal_status', ''))
    deal_stage = row.get('Deal Stage', row.get('deal_stage', ''))
    authoritative_status, status_anomalies = validate_deal_status_stage(deal_status, deal_stage)
    cleaned['deal_status'] = authoritative_status
    cleaned['deal_stage'] = normalize_text(deal_stage)
    anomalies.extend(status_anomalies)
    
    # Handle financial fields
    amount_billed = normalize_currency(row.get('Amount to be billed', row.get('amount_to_billed', '')))
    amount_receivable = normalize_currency(row.get('Amount Receivable', row.get('amount_receivable', '')))
    deal_value = normalize_currency(row.get('Masked Deal value', row.get('masked_deal_value', '')))
    
    cleaned['amount_to_billed'] = amount_billed
    cleaned['amount_receivable'] = amount_receivable
    cleaned['masked_deal_value'] = deal_value
    
    # Validate financial invariants
    financial_anomalies = validate_financial_invariants(amount_billed, amount_receivable)
    for anomaly in financial_anomalies:
        anomaly.row_id = row_id
        anomalies.append(anomaly)
    
    # Handle closure probability
    closure_prob = row.get('Closure Probability', row.get('closure_probability', ''))
    cleaned['closure_probability'] = normalize_currency(closure_prob)
    
    # Calculate stage probability for pipeline
    stage_prob, used_fallback = get_stage_probability(deal_stage, closure_prob)
    cleaned['stage_probability'] = stage_prob
    cleaned['used_probability_fallback'] = used_fallback
    
    # Add row identifier for anomaly tracking
    for anomaly in anomalies:
        if anomaly.row_id == "unknown":
            anomaly.row_id = row_id
    
    return cleaned, anomalies


def normalize_work_orders_row(row: Dict[str, Any], row_id: str) -> Tuple[Dict[str, Any], List[Anomaly]]:
    """
    Normalize a single work orders row.
    Returns (cleaned_row, anomalies).
    """
    cleaned = {}
    anomalies = []
    
    # Normalize basic fields
    cleaned['work_order_number'] = normalize_text(
        row.get('Serial #', row.get('Work Order Number', row.get('work_order_number', '')))
    )
    cleaned['client_code'] = normalize_text(row.get('Client Code', row.get('client_code', '')))
    cleaned['sector'] = normalize_text(row.get('Sector', row.get('sector', '')))
    cleaned['client_name'] = normalize_text(row.get('Customer Name Code', row.get('Client Name', row.get('client_name', ''))))
    cleaned['owner'] = normalize_text(row.get('BD/KAM Personnel code', row.get('Owner', row.get('owner', ''))))
    
    # Handle multi-select fields
    cleaned['type_of_work'] = normalize_multi_select(
        row.get('Type of Work', row.get('type_of_work', ''))
    )
    
    # Handle embedded units
    quantities = row.get('Quantities as per PO', row.get('quantities_as_per_po', ''))
    quantity_value, quantity_unit = normalize_embedded_unit(quantities)
    cleaned['quantity_value'] = quantity_value
    cleaned['quantity_unit'] = quantity_unit
    
    if quantity_value is None and not is_null_sentinel(quantities):
        anomalies.append(Anomaly(
            row_id=row_id,
            type=AnomalyType.PARSE_FAILURE,
            detail=f"Could not parse embedded unit field: {quantities}",
            field="quantities_as_per_po"
        ))
    
    # Handle dates
    cleaned['invoice_date'] = normalize_date(row.get('Last invoice date', row.get('Invoice Date', row.get('invoice_date', ''))))
    cleaned['work_order_date'] = normalize_date(
        row.get('Date of PO/LOI', row.get('Work Order Date', row.get('work_order_date', '')))
    )
    
    # Handle financial fields
    amount_billed = normalize_currency(row.get('Amount to be billed in Rs. (Exl. of GST) (Masked)', row.get('Amount to be billed', row.get('amount_to_billed', ''))))
    amount_receivable = normalize_currency(row.get('Amount Receivable (Masked)', row.get('Amount Receivable', row.get('amount_receivable', ''))))
    
    cleaned['amount_to_billed'] = amount_billed
    cleaned['amount_receivable'] = amount_receivable
    
    # Validate financial invariants
    financial_anomalies = validate_financial_invariants(amount_billed, amount_receivable)
    for anomaly in financial_anomalies:
        anomaly.row_id = row_id
        anomalies.append(anomaly)
    
    # Handle unified billing status
    invoice_status = row.get('Invoice Status', row.get('invoice_status', ''))
    wo_status = row.get('WO Status (billed)', row.get('wo_status_billed', ''))
    billing_status = row.get('Billing Status', row.get('billing_status', ''))
    
    unified_status, billing_anomalies = unified_billing_status(invoice_status, wo_status, billing_status)
    cleaned['unified_billing_status'] = unified_status
    cleaned['invoice_status'] = normalize_text(invoice_status)
    cleaned['wo_status_billed'] = normalize_text(wo_status)
    cleaned['billing_status'] = normalize_text(billing_status)
    
    for anomaly in billing_anomalies:
        anomaly.row_id = row_id
        anomalies.append(anomaly)
    
    return cleaned, anomalies


def normalize_deals_batch(rows: List[Dict[str, Any]]) -> DataQualityReport:
    """
    Normalize a batch of deals rows.
    Returns DataQualityReport with cleaned records and anomalies.
    """
    cleaned_records = []
    all_anomalies = []
    excluded_count = 0
    
    for i, row in enumerate(rows):
        row_id = f"deal_{i}"
        cleaned, anomalies = normalize_deals_row(row, row_id)
        
        # Exclude rows with critical invalid values
        has_critical_invalid = any(
            a.type == AnomalyType.INVALID_VALUE and 
            a.field in ['amount_to_billed', 'amount_receivable']
            for a in anomalies
        )
        
        if has_critical_invalid:
            excluded_count += 1
        else:
            cleaned_records.append(cleaned)
        
        all_anomalies.extend(anomalies)
    
    total_rows = len(rows)
    excluded_fraction = excluded_count / total_rows if total_rows > 0 else 0
    
    return DataQualityReport(
        cleaned_records=cleaned_records,
        excluded_count=excluded_count,
        excluded_fraction=excluded_fraction,
        anomalies=all_anomalies,
        total_rows=total_rows
    )


def normalize_work_orders_batch(rows: List[Dict[str, Any]]) -> DataQualityReport:
    """
    Normalize a batch of work orders rows.
    Returns DataQualityReport with cleaned records and anomalies.
    """
    cleaned_records = []
    all_anomalies = []
    excluded_count = 0
    
    for i, row in enumerate(rows):
        row_id = f"wo_{i}"
        cleaned, anomalies = normalize_work_orders_row(row, row_id)
        
        # Exclude rows with critical invalid values
        has_critical_invalid = any(
            a.type == AnomalyType.INVALID_VALUE and 
            a.field in ['amount_to_billed', 'amount_receivable']
            for a in anomalies
        )
        
        if has_critical_invalid:
            excluded_count += 1
        else:
            cleaned_records.append(cleaned)
        
        all_anomalies.extend(anomalies)
    
    total_rows = len(rows)
    excluded_fraction = excluded_count / total_rows if total_rows > 0 else 0
    
    return DataQualityReport(
        cleaned_records=cleaned_records,
        excluded_count=excluded_count,
        excluded_fraction=excluded_fraction,
        anomalies=all_anomalies,
        total_rows=total_rows
    )


def drop_null_columns(records: List[Dict[str, Any]], threshold: float = 0.95) -> List[Dict[str, Any]]:
    """
    Drop columns that are ~100% null (above threshold) from records.
    This prevents near-empty columns from reaching the LLM's context.
    """
    if not records:
        return records
    
    # Calculate null fraction for each column
    column_null_fractions = {}
    for record in records:
        for key, value in record.items():
            if key not in column_null_fractions:
                column_null_fractions[key] = {'null_count': 0, 'total_count': 0}
            
            column_null_fractions[key]['total_count'] += 1
            if is_null_sentinel(value):
                column_null_fractions[key]['null_count'] += 1
    
    # Identify columns to drop
    columns_to_drop = []
    for column, stats in column_null_fractions.items():
        null_fraction = stats['null_count'] / stats['total_count'] if stats['total_count'] > 0 else 0
        if null_fraction >= threshold:
            columns_to_drop.append(column)
    
    # Drop columns from records
    cleaned_records = []
    for record in records:
        cleaned_record = {k: v for k, v in record.items() if k not in columns_to_drop}
        cleaned_records.append(cleaned_record)
    
    return cleaned_records
