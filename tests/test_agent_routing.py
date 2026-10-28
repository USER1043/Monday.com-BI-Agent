"""
Agent routing and session behaviour that doesn't need the LLM: intent
detection, quarter resolution, per-session settings, tool-argument parsing,
and the Monday.com read cache.
"""

from datetime import date

import pytest

import data_source
import tools
from agent import GroqAgent, create_agent
from leadership_update import is_leadership_update_request
from tools import OrgSettings, get_org_settings, set_org_setting, use_org_settings


@pytest.fixture
def agent():
    # No network: the OpenAI client is only constructed, and data comes from Excel
    return create_agent("test-key", org_settings=OrgSettings(settings_file=None))


@pytest.mark.parametrize("message,expected", [
    ("Give me this week's leadership update", True),
    ("weekly update please", True),
    ("Executive update for the board", True),
    ("Give me an update on Mining deals", False),
    ("What's the status report on billing?", False),
    ("Executive summary of the renewables pipeline", False),
])
def test_leadership_update_detection(message, expected):
    assert is_leadership_update_request(message) is expected


@pytest.mark.parametrize("message,expected", [
    ("How's our pipeline this quarter?", True),
    ("Show me quarterly revenue", True),
    ("What closed in Q1?", True),
    ("Compare the last two quarters", True),
    ("What's happening at our headquarters project?", False),
    ("Show me deals in the Mining sector", False),
])
def test_quarter_reference_detection(agent, message, expected):
    assert agent._detect_quarter_reference(message) is expected


@pytest.mark.parametrize("settings,today,description", [
    ({"fiscal_quarter_convention": "calendar"}, date(2026, 10, 28), "Calendar Q4 (2026-10-01 to 2026-12-31)"),
    ({"fiscal_quarter_convention": "calendar"}, date(2026, 2, 1), "Calendar Q1 (2026-01-01 to 2026-03-31)"),
    ({"fiscal_quarter_convention": "fiscal", "fiscal_year_start_month": "4"}, date(2026, 10, 28),
     "Fiscal Q3 (2026-10-01 to 2026-12-31)"),
    ({"fiscal_quarter_convention": "fiscal", "fiscal_year_start_month": "4"}, date(2026, 2, 15),
     "Fiscal Q4 (2026-01-01 to 2026-03-31)"),
    ({"fiscal_quarter_convention": "fiscal", "fiscal_year_start_month": "11"}, date(2026, 12, 5),
     "Fiscal Q1 (2026-11-01 to 2027-01-31)"),
    ({"fiscal_quarter_convention": "fiscal", "fiscal_year_start_month": "2"}, date(2026, 1, 31),
     "Fiscal Q4 (2025-11-01 to 2026-01-31)"),
])
def test_quarter_range_resolution(agent, settings, today, description):
    for key, value in settings.items():
        agent.org_settings.set_setting(key, value)
    with use_org_settings(agent.org_settings):
        assert agent._resolve_quarter_date_range(today)["description"] == description


def test_settings_are_isolated_per_session():
    shared_before = tools.org_settings.get_all_settings()
    first = create_agent("test-key", org_settings=OrgSettings(settings_file=None))
    second = create_agent("test-key", org_settings=OrgSettings(settings_file=None))

    with use_org_settings(first.org_settings):
        set_org_setting("fiscal_quarter_convention", "fiscal")
        assert get_org_settings().data == {"fiscal_quarter_convention": "fiscal"}

    with use_org_settings(second.org_settings):
        assert get_org_settings().data == {}
    # Outside a session the file-backed default store is active and untouched
    assert get_org_settings().data == shared_before


def test_convention_answer_is_stored_in_the_session(agent):
    question = agent.process_message("How's our pipeline this quarter?")
    assert question.clarifying_question_asked
    follow_up = agent.process_message("fiscal")
    assert "which month" in follow_up.content.lower()
    assert agent.org_settings.get_all_settings() == {"fiscal_quarter_convention": "fiscal"}


@pytest.mark.parametrize("raw,expected", [
    ('{"sector": "Mining"}', {"sector": "Mining"}),
    ("", {}),
    ("{sector: Mining", None),
    ('["Mining"]', None),
])
def test_parse_tool_arguments(raw, expected):
    assert GroqAgent._parse_tool_arguments(raw) == expected


def test_mcp_reads_are_cached(monkeypatch):
    calls = []

    def fake_mcp_read():
        calls.append(1)
        return [{"Deal Name": "Naruto"}]

    monkeypatch.setenv("DATA_SOURCE", "monday")
    monkeypatch.setenv("DATA_CACHE_SECONDS", "120")
    monkeypatch.setattr(data_source, "sync_get_deals_data", fake_mcp_read)
    data_source.clear_cache()

    first = data_source.get_deals_data()
    first[0]["Deal Name"] = "mutated"
    assert data_source.get_deals_data() == [{"Deal Name": "Naruto"}]
    assert len(calls) == 1

    data_source.clear_cache()
    data_source.get_deals_data()
    assert len(calls) == 2


def test_mcp_cache_can_be_disabled(monkeypatch):
    calls = []
    monkeypatch.setenv("DATA_SOURCE", "monday")
    monkeypatch.setenv("DATA_CACHE_SECONDS", "0")
    monkeypatch.setattr(data_source, "sync_get_deals_data", lambda: calls.append(1) or [])
    data_source.clear_cache()

    data_source.get_deals_data()
    data_source.get_deals_data()
    assert len(calls) == 2
