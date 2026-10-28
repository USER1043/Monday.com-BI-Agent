"""
Agent Loop for Monday.com Business Intelligence Agent

Orchestrates gpt-oss-120b via Groq API with tool calling.
Per BUILD_BRIEF.md Section 5:
- Model: gpt-oss-120b via Groq API
- Test tool-calling reliability early
- System prompt with domain vocabulary and behaviors
- Quarter convention stateful behavior
- Always surface anomalies/disclosures
- Ask one clarifying question when genuinely ambiguous
- Never claim Deals↔Work Orders record-level relationship
"""

import os
import json
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass
import re
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

try:
    from openai import OpenAI, RateLimitError
except ImportError:
    print("Warning: OpenAI package not installed. Install with: pip install openai")
    OpenAI = None
    RateLimitError = None

from tools import (
    TOOL_FUNCTIONS, TOOL_DEFINITIONS,
    OrgSettings, org_settings as default_org_settings, use_org_settings,
    get_org_settings, set_org_setting,
    get_deals, get_work_orders, format_money_fields
)
from leadership_update import generate_leadership_update, is_leadership_update_request


@dataclass
class AgentMessage:
    """Represents a message in the conversation."""
    role: str  # "user", "assistant", "system"
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_results: Optional[List[Dict[str, Any]]] = None


@dataclass
class AgentResponse:
    """Represents the agent's response."""
    content: str
    tool_calls_made: List[str]
    data_quality_info: List[Dict[str, Any]]
    clarifying_question_asked: bool


class GroqAgent:
    """Agent that uses gpt-oss-120b via Groq API with tool calling."""
    
    def __init__(self, api_key: str, org_settings: Optional[OrgSettings] = None):
        if OpenAI is None:
            raise RuntimeError("OpenAI package not installed")
        
        self.client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key,
            # Groq's free tier rate-limits bursts; the SDK backs off and retries
            max_retries=4
        )
        # Quarter convention etc.; the app passes an in-memory store per session
        self.org_settings = org_settings or default_org_settings
        self.model = "openai/gpt-oss-120b"
        self.conversation_history: List[AgentMessage] = []
        self._domain_vocab_loaded = False
        self._domain_vocabulary = {}
        self._domain_vocabulary_str = ""
        # Quarter conventions need persistence. Other clarifications are kept in
        # the normal conversation history so the model can resolve any kind of
        # ambiguity from the user's next message.
        self._pending_clarification_kind: Optional[str] = None
        self._pending_question: Optional[str] = None
        
        # Initialize system prompt after all attributes are set
        self.system_prompt = self._build_system_prompt()
    
    def _build_system_prompt(self) -> str:
        """Build the system prompt with domain knowledge and behaviors."""
        base_prompt = """You are a Business Intelligence Agent for Skylark Drones. You help founders analyze deals and work orders data from Monday.com.

CORE BEHAVIORS:
1. ALWAYS call the relevant tool for every new question involving deals or work orders. NEVER answer from a previous tool result, even if the question looks similar.
2. Communicate at an executive/founder level. Focus on business insights, total metrics, and actionable trends. Do not discuss raw data issues, null values, sparsity, or token limits unless explicitly asked.
3. Ask exactly ONE concise, specific clarifying question when any query intent, scope, metric, filter, or date range is genuinely ambiguous and there is no stored convention that resolves it. Examples include an unknown sector, an unclear date range, or whether "pipeline" means value or count; these are examples, not an exhaustive list. Do not ask a clarification when a reasonable default is defined below: state that assumption and proceed instead. Do not call tools until the clarification is answered.
4. Never claim a record-level relationship between Deals and Work Orders boards. They can only be compared by shared dimensions (sector, owner), not linked record-to-record.
5. When discussing "this quarter" or similar relative periods, first check org settings for fiscal quarter convention. If not set, ask the user: "calendar quarter, or fiscal (if fiscal, which month does the FY start)?" Then persist the answer.

DOMAIN KNOWLEDGE:
"""
        
        # Domain vocabulary will be loaded dynamically
        prompt = base_prompt + self._load_domain_vocabulary()
        
        prompt += """

PIPELINE DEFAULTS:
- "Pipeline value" defaults to active pipeline only: Deal Status = Open, valued by Masked Deal value, placed in-period by Tentative Close Date.
- "Weighted pipeline value" is Masked Deal value × stage probability and is already computed by the deal tools (`total_weighted_pipeline_value`). Do not ask the user for probabilities.
- Won and Dead deals are excluded from this default. On Hold deals are shown separately, not dropped.
- Broader views (include Won/Dead/On Hold) are available on explicit request; pass status "All" for every status (e.g. win rates, totals across all deals).
- Weighted pipeline only covers Open deals in stages A-F. Whenever you report a weighted pipeline figure you MUST also state how many of those deals used the stage-based probability fallback (`probability_fallback` in the tool output).
- For "typical" or "average" deal size, lead with the median (`median_deal_value`); deal values are heavily skewed, so if you mention the mean, show it next to the median.
- Deal tools return `overdue_open_deals`: Open deals whose Tentative Close Date has already passed. When it has a non-zero count, report it alongside the in-period figures as slipped deals that still need a new close date (count, total value, latest close date). If the in-period pipeline is empty, lead with this instead of only saying there is no pipeline.

DATA QUALITY HANDLING:
- Tools return data quality reports with anomalies. Do not expose these technical reports to the founder unless it drastically impacts the core metric being asked (e.g., if everything is empty). Keep the focus on the business numbers.
- Exceptions you must always surface, briefly and in business terms:
  * Work orders excluded for invalid (negative) amounts (`excluded_invalid_rows` > 0): state how many were left out of the totals.
  * Any `*_disclosure` with disclosure_type "estimate_range": give the confirmed figure and the labeled estimated upper bound.
  * "quiet_footnote" disclosures with excluded_count > 0: one short footnote line.

QUARTER CONVENTION:
- The first time a quarter-dependent query comes in, ask whether they use calendar quarters or fiscal quarters (and which month starts the fiscal year)
- Use get_org_settings() to check if this is already set
- Use set_org_setting() to persist the answer
- Every answer that resolves "this quarter" should state the exact date range used

TOOLS AVAILABLE:
"""
        
        # Add tool descriptions
        for tool_def in TOOL_DEFINITIONS:
            tool_name = tool_def['function']['name'] if 'function' in tool_def else tool_def['name']
            tool_desc = tool_def['function']['description'] if 'function' in tool_def else tool_def['description']
            prompt += f"- {tool_name}: {tool_desc}\n"
        
        prompt += """

YOUR ROLE:
You are a top-tier Business Intelligence executive assistant for the founders of Skylark Drones. Your answers must be concise, direct, confident, and heavily focused on the core numbers and business insights. 
- Use the metrics tools (get_deal_metrics, get_work_order_metrics) to give precise totals instead of trying to sum rows yourself.
- NEVER speak like a Data Scientist investigating a dataset. Do NOT mention "rows", "nulls", "sparsity", "API limits", or "tokens". 
- STRICT RULE: NEVER mention internal tool names (e.g., 'get_deals', 'get_deal_metrics'). NEVER mention internal backend column names (e.g., 'masked_deal_value', 'stage_probability'). NEVER explain the mathematical formulas or queries used to get the data.
- Do NOT create tables with "Notes" or "Comments" columns that expose how you processed the data. Just present the final business figures cleanly.
- CURRENCY: All monetary values are in Indian Rupees and tool outputs already format them in crore (Cr), lakh (L) or rupees. Copy amounts exactly as the tools give them; never convert units yourself, never use millions, never use $.
- Deliver your insights in a clean, professional, board-ready format.
"""
        
        return prompt
    
    def _load_domain_vocabulary(self) -> str:
        """Load domain vocabulary (sectors, stages, statuses) from the live board data."""
        if self._domain_vocab_loaded:
            return self._domain_vocabulary_str
        
        try:
            deals = get_deals(status="All")
            work_orders = get_work_orders()
            if deals.success and work_orders.success:
                def values(records: List[Dict[str, Any]], field: str) -> str:
                    return ", ".join(sorted({r[field] for r in records if r.get(field)}))
                
                vocab = "\nACTUAL DOMAIN VALUES FROM BOARDS (use these exact values in tool filters):\n"
                vocab += "Deals Board:\n"
                vocab += f"- Sectors: {values(deals.data, 'sector')}\n"
                vocab += f"- Deal Stages (in order): {values(deals.data, 'deal_stage')}\n"
                vocab += f"- Deal Status values: {values(deals.data, 'deal_status')}\n"
                vocab += f"- Owners: {values(deals.data, 'owner')}\n"
                vocab += "Work Orders Board:\n"
                vocab += f"- Sectors: {values(work_orders.data, 'sector')}\n"
                vocab += f"- Unified Billing Status values: {values(work_orders.data, 'unified_billing_status')}\n"
                vocab += "  (Invoice Status first, then WO Status (billed) Open/Closed, then Billing Status)\n"
                
                self._domain_vocabulary_str = vocab
                self._domain_vocab_loaded = True
                return vocab
            
        except Exception as e:
            print(f"Warning: Could not load domain vocabulary: {e}")
        
        # Fallback vocabulary
        fallback = """
ACTUAL DOMAIN VALUES FROM BOARDS:
Deals Board:
- Deal Stages: A. Lead Generated, B. Sales Qualified Leads, C. Demo Done, D. Feasibility, E. Proposal/Commercials Sent, F. Negotiations
- Deal Status: Open, Won, Dead, On Hold
Work Orders Board:
- Unified Billing Status: Derived from Invoice Status > WO Status > Billing Status
"""
        self._domain_vocabulary_str = fallback
        self._domain_vocab_loaded = True
        return fallback
    
    def _check_quarter_convention(self) -> Optional[str]:
        """Check if quarter convention is set, return question if not."""
        settings = get_org_settings()
        if settings.success:
            quarter_setting = settings.data.get("fiscal_quarter_convention")
            fy_start = settings.data.get("fiscal_year_start_month")
            
            # Check if either convention is set (calendar or fiscal with start month)
            if quarter_setting == "calendar":
                return None  # Already set to calendar
            elif quarter_setting == "fiscal" and fy_start:
                return None  # Already set to fiscal with start month
            elif quarter_setting == "fiscal" and not fy_start:
                return "When you say 'this quarter', which month does your fiscal year start?"
            else:
                return "When you say 'this quarter', do you mean calendar quarter, or fiscal quarter (and if fiscal, which month does your fiscal year start)?"
        return "When you say 'this quarter', do you mean calendar quarter, or fiscal quarter (and if fiscal, which month does your fiscal year start)?"
    
    @staticmethod
    def _fiscal_year_start_month() -> int:
        """Month the (fiscal) year starts in under the stored convention; 1 for calendar."""
        settings = get_org_settings()
        data = settings.data if settings.success else {}
        if data.get("fiscal_quarter_convention") == "fiscal":
            return int(data.get("fiscal_year_start_month", 1))
        return 1
    
    def _resolve_quarter_date_range(self, today: Optional[date] = None) -> Dict[str, str]:
        """Resolve the current quarter's date range under the stored convention."""
        settings = get_org_settings()
        convention = (settings.data if settings.success else {}).get("fiscal_quarter_convention", "calendar")
        fy_start_month = self._fiscal_year_start_month()
        today = today or date.today()
        
        months_into_year = (today.month - fy_start_month) % 12
        start_year, start_month = today.year, today.month - months_into_year % 3
        if start_month <= 0:
            start_year, start_month = start_year - 1, start_month + 12
        end_year, end_month = start_year, start_month + 3
        if end_month > 12:
            end_year, end_month = end_year + 1, end_month - 12
        
        start_date = date(start_year, start_month, 1).isoformat()
        end_date = (date(end_year, end_month, 1) - timedelta(days=1)).isoformat()
        quarter_number = months_into_year // 3 + 1
        
        return {
            "date_from": start_date,
            "date_to": end_date,
            "convention": convention,
            "description": f"{convention.capitalize()} Q{quarter_number} ({start_date} to {end_date})"
        }
    
    def _date_context(self) -> str:
        """Today's date and the current quarter, so relative periods resolve correctly."""
        context = f"\n\nTODAY'S DATE: {date.today().isoformat()}\n"
        if self._check_quarter_convention() is None:
            today = date.today()
            quarter = self._resolve_quarter_date_range(today)
            context += (
                f"CURRENT QUARTER (org convention): {quarter['description']}. "
                "Use exactly this range for \"this quarter\"; shift it by 3 months for last/next quarter.\n"
            )
            # The current (fiscal) year's quarters, so named quarters resolve without re-asking
            fy_start_month = self._fiscal_year_start_month()
            fy_start = date(today.year if today.month >= fy_start_month else today.year - 1, fy_start_month, 1)
            quarters = []
            for _ in range(4):
                q = self._resolve_quarter_date_range(fy_start)
                quarters.append(q["description"])
                fy_start = date.fromisoformat(q["date_to"]) + timedelta(days=1)
            context += (
                f"QUARTERS OF THE CURRENT YEAR UNDER THIS CONVENTION: {'; '.join(quarters)}.\n"
                "Apply the stored convention to every quarter reference, including named ones like "
                "\"Q1 2026\"; resolve them yourself instead of asking again, and state the date range used. "
                "For a fiscal quarter in another year, assume the fiscal year is named after the calendar "
                "year it starts in and say so.\n"
            )
        return context
    
    def _detect_quarter_reference(self, user_message: str) -> bool:
        """Detect if user message contains quarter reference."""
        # Whole words only, so e.g. "headquarters" doesn't count
        return re.search(r"\bquarter(s|ly)?\b|\bq[1-4]\b", user_message.lower()) is not None
    
    def _process_quarter_convention_answer(self, user_message: str) -> bool:
        """
        Process user's answer to quarter convention question.
        Returns True if the message was a quarter convention answer.
        """
        if self._pending_clarification_kind != "quarter":
            return False

        user_message_lower = user_message.lower()
        months = ["january", "february", "march", "april", "may", "june", 
                 "july", "august", "september", "october", "november", "december"]
        month_match = re.search(r"\b(" + "|".join(months) + r")\b", user_message_lower)
        current = get_org_settings()
        already_fiscal = current.success and current.data.get("fiscal_quarter_convention") == "fiscal"
        
        # Check if this is an answer to the quarter convention question
        if "calendar" in user_message_lower:
            set_org_setting("fiscal_quarter_convention", "calendar")
            return True
        elif "fiscal" in user_message_lower or (already_fiscal and month_match):
            # A bare month answers the follow-up "which month does your FY start?"
            set_org_setting("fiscal_quarter_convention", "fiscal")
            if month_match:
                set_org_setting("fiscal_year_start_month", str(months.index(month_match.group(1)) + 1))
            return True
        
        return False

    @staticmethod
    def _is_clarifying_question(content: str, tool_calls_made: List[str]) -> bool:
        """Identify a model response that pauses for one user-supplied detail."""
        return not tool_calls_made and content.strip().endswith("?")
    
    def _format_tool_result(self, tool_name: str, result: Dict[str, Any]) -> str:
        """Format tool result for inclusion in response."""
        if not result.get("success"):
            return f"Error calling {tool_name}: {result.get('error')}"
        
        output = f"Tool: {tool_name}\n"
        
        data = format_money_fields(result.get("data"))
        if data:
            if tool_name == "get_board_schemas" and isinstance(data, dict):
                # Strip out bulky settings (labels, colors) from schema to save tokens
                compact_schema = {}
                for board, cols in data.items():
                    if isinstance(cols, list):
                        compact_schema[board] = [
                            {"id": c.get("id"), "title": c.get("title"), "type": c.get("type")}
                            for c in cols if isinstance(c, dict)
                        ]
                output += f"Data: {json.dumps(compact_schema, default=str, ensure_ascii=False)}\n"
            elif isinstance(data, list):
                output += f"Returned {len(data)} records\n"
                if len(data) > 0:
                    import csv
                    import io
                    csv_out = io.StringIO()
                    # Only include keys that have values to minimize tokens
                    keys = set()
                    for r in data:
                        for k, v in r.items():
                            if v is not None and str(v).strip() != "":
                                keys.add(k)
                    if keys:
                        writer = csv.DictWriter(csv_out, fieldnames=list(keys), extrasaction='ignore')
                        writer.writeheader()
                        
                        # Limit to 25 records to prevent Token Limit (413) errors
                        max_records = 25
                        writer.writerows(data[:max_records])
                        
                        output += f"Data (CSV format, showing first {min(len(data), max_records)} of {len(data)} records):\n{csv_out.getvalue()}\n"
                        if len(data) > max_records:
                            output += f"...(Truncated {len(data) - max_records} records to save tokens). Use the 'aggregations' metadata below for totals.\n"
            else:
                output += f"Data: {json.dumps(data, indent=2, default=str, ensure_ascii=False)}\n"
        
        qr = result.get("quality_report")
        if qr:
            output += f"\nData Quality Report:\n"
            output += f"- Total rows: {qr.total_rows}\n"
            output += f"- Cleaned rows: {len(qr.cleaned_records)}\n"
            output += f"- Excluded: {qr.excluded_count} ({qr.excluded_fraction:.1%})\n"
            output += f"- Anomalies: {len(qr.anomalies)}\n"
            
            if qr.anomalies:
                output += f"- Sample anomalies:\n"
                for anomaly in qr.anomalies[:3]:
                    output += f"  * {anomaly.type.value if hasattr(anomaly.type, 'value') else str(anomaly.type)}: {anomaly.detail}\n"
        
        metadata = format_money_fields(result.get("metadata"))
        if metadata:
            output += f"\nMetadata: {json.dumps(metadata, indent=2, default=str, ensure_ascii=False)}\n"
        
        return output
    
    @staticmethod
    def _parse_tool_arguments(raw: Optional[str]) -> Optional[Dict[str, Any]]:
        """Parse a tool call's JSON arguments; None if they aren't a JSON object."""
        if not raw:
            return {}
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return args if isinstance(args, dict) else None
    
    def _execute_tool_call(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool call and return the result."""
        if tool_name not in TOOL_FUNCTIONS:
            return {
                "error": f"Unknown tool: {tool_name}",
                "success": False
            }
        
        try:
            tool_func = TOOL_FUNCTIONS[tool_name]
            result = tool_func(**tool_args)
            
            return {
                "success": result.success,
                "data": result.data,
                "quality_report": result.quality_report,
                "error": result.error,
                "metadata": result.metadata
            }
        except Exception as e:
            return {
                "error": str(e),
                "success": False
            }
    
    def process_message(self, user_message: str) -> AgentResponse:
        """Process a user message and return the agent's response."""
        with use_org_settings(self.org_settings):
            return self._process_message(user_message)
    
    def _process_message(self, user_message: str) -> AgentResponse:
        # Check for leadership update request
        if is_leadership_update_request(user_message):
            self.conversation_history.append(AgentMessage(role="user", content=user_message))
            try:
                leadership_update = generate_leadership_update()
                self.conversation_history.append(AgentMessage(role="assistant", content=leadership_update))
                return AgentResponse(
                    content=leadership_update,
                    tool_calls_made=["get_deals", "get_work_orders", "get_data_quality_summary", "get_org_settings"],
                    data_quality_info=[],
                    clarifying_question_asked=False
                )
            except Exception as e:
                error_msg = f"Error generating leadership update: {str(e)}"
                self.conversation_history.append(AgentMessage(role="assistant", content=error_msg))
                return AgentResponse(
                    content=error_msg,
                    tool_calls_made=[],
                    data_quality_info=[],
                    clarifying_question_asked=False
                )
        
        # Check if user is answering the quarter convention question
        if self._process_quarter_convention_answer(user_message):
            self.conversation_history.append(AgentMessage(role="user", content=user_message))
            follow_up = self._check_quarter_convention()
            if follow_up:
                # Fiscal year chosen without a start month: ask for it
                self.conversation_history.append(AgentMessage(role="assistant", content=follow_up))
                return AgentResponse(
                    content=follow_up,
                    tool_calls_made=[],
                    data_quality_info=[],
                    clarifying_question_asked=True
                )
            self._pending_clarification_kind = None
            original_question, self._pending_question = self._pending_question, None
            if original_question:
                # Answer the question that triggered the clarification
                return self._process_normal_query(original_question)
            confirmation = "Got it! I'll use that convention for quarter-based questions going forward."
            self.conversation_history.append(AgentMessage(role="assistant", content=confirmation))
            return AgentResponse(
                content=confirmation,
                tool_calls_made=[],
                data_quality_info=[],
                clarifying_question_asked=False
            )
        
        # Any other message means the user moved on from a pending clarification
        self._pending_clarification_kind = None
        self._pending_question = None
        
        # Check for quarter convention (only for non-leadership-update queries)
        quarter_question = None
        if self._detect_quarter_reference(user_message):
            quarter_question = self._check_quarter_convention()
        
        # If quarter convention needs to be set, ask first
        if quarter_question:
            self._pending_clarification_kind = "quarter"
            self._pending_question = user_message
            self.conversation_history.append(AgentMessage(role="user", content=user_message))
            self.conversation_history.append(AgentMessage(role="assistant", content=quarter_question))
            return AgentResponse(
                content=quarter_question,
                tool_calls_made=[],
                data_quality_info=[],
                clarifying_question_asked=True
            )
        
        # Process normal query
        return self._process_normal_query(user_message)
    
    def _process_normal_query(self, user_message: str) -> AgentResponse:
        """Process a normal query without quarter convention handling."""
        # Add user message to history
        self.conversation_history.append(AgentMessage(role="user", content=user_message))
        
        # Prepare messages for API
        messages = [
            {"role": "system", "content": self.system_prompt + self._date_context()}
        ]
        
        # Add conversation history
        for msg in self.conversation_history:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Make API call with tools
        tool_calls_made = []
        data_quality_info = []
        max_iterations = 5  # Prevent infinite loops
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto"
                )
                
                assistant_message = response.choices[0].message
                
                # Check if model wants to call tools
                if assistant_message.tool_calls and len(assistant_message.tool_calls) > 0:
                    # Execute tool calls
                    tool_results = []
                    for tool_call in assistant_message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = self._parse_tool_arguments(tool_call.function.arguments)
                        
                        tool_calls_made.append(tool_name)
                        if tool_args is None:
                            # Running the tool unfiltered would answer a different question
                            result = {
                                "success": False,
                                "error": "Tool arguments were not valid JSON. Call the tool again with a JSON object of arguments."
                            }
                        else:
                            result = self._execute_tool_call(tool_name, tool_args)
                        
                        # Extract data quality info
                        if result.get("quality_report"):
                            qr = result["quality_report"]
                            data_quality_info.append({
                                "tool": tool_name,
                                "anomalies": len(qr.anomalies),
                                "excluded_count": qr.excluded_count,
                                "excluded_fraction": qr.excluded_fraction
                            })
                        
                        tool_results.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "content": self._format_tool_result(tool_name, result)
                        })
                    
                    # Tool calls and their results stay in this request only. The
                    # persistent history keeps user turns and final answers, so a
                    # later request never carries tool calls without their results
                    # and every new question re-calls the tools.
                    messages.append({
                        "role": "assistant",
                        "content": assistant_message.content or "",
                        "tool_calls": [tc.model_dump() for tc in assistant_message.tool_calls]
                    })
                    
                    for tr in tool_results:
                        messages.append(tr)
                    
                    # Continue loop to let model process tool results
                    continue
                
                else:
                    # Model provided final response
                    final_content = assistant_message.content or ""
                    
                    # Add assistant response to history
                    self.conversation_history.append(
                        AgentMessage(role="assistant", content=final_content)
                    )
                    

                    return AgentResponse(
                        content=final_content,
                        tool_calls_made=tool_calls_made,
                        data_quality_info=data_quality_info,
                        clarifying_question_asked=self._is_clarifying_question(
                            final_content, tool_calls_made
                        )
                    )
            
            except Exception as e:
                if RateLimitError is not None and isinstance(e, RateLimitError):
                    error_msg = ("The AI service is briefly rate-limited (Groq free tier). "
                                 "Please wait about a minute and ask again.")
                else:
                    error_msg = f"Error processing message: {str(e)}"
                self.conversation_history.append(AgentMessage(role="assistant", content=error_msg))
                return AgentResponse(
                    content=error_msg,
                    tool_calls_made=tool_calls_made,
                    data_quality_info=data_quality_info,
                    clarifying_question_asked=False
                )
        
        # Max iterations reached
        timeout_msg = "I apologize, but I'm having trouble completing your request. Let me try a different approach."
        self.conversation_history.append(AgentMessage(role="assistant", content=timeout_msg))
        return AgentResponse(
            content=timeout_msg,
            tool_calls_made=tool_calls_made,
            data_quality_info=data_quality_info,
            clarifying_question_asked=False
        )
    
    def reset_conversation(self):
        """Reset the conversation history."""
        self.conversation_history = []
        self._pending_clarification_kind = None
        self._pending_question = None


def create_agent(api_key: str, org_settings: Optional[OrgSettings] = None) -> GroqAgent:
    """Factory function to create a GroqAgent instance."""
    return GroqAgent(api_key, org_settings)


def test_tool_calling_reliability(agent: GroqAgent) -> bool:
    """
    Test tool-calling reliability before building the full agent loop.
    Per BUILD_BRIEF.md Section 5: Test early before building anything on top.
    """
    print("Testing tool-calling reliability...")
    
    test_queries = [
        "What's our total pipeline value?",
        "How many deals do we have in the Negotiations stage?",
        "What's our pipeline value for the Renewables sector?"
    ]
    
    for i, query in enumerate(test_queries):
        print(f"\nTest query {i+1}: {query}")
        agent.reset_conversation()
        
        try:
            response = agent.process_message(query)
            print(f"Response: {response.content[:200]}...")
            print(f"Tools called: {response.tool_calls_made}")
            
            # Check if tools were actually called
            if not response.tool_calls_made:
                print("WARNING: Model did not call any tools for this query!")
                return False
            
            # Test follow-up query with different filter
            follow_up = "Now show me deals in the Construction sector"
            print(f"\nFollow-up query: {follow_up}")
            response2 = agent.process_message(follow_up)
            print(f"Response: {response2.content[:200]}...")
            print(f"Tools called: {response2.tool_calls_made}")
            
            # Check if model re-called tools for the different query
            if not response2.tool_calls_made:
                print("WARNING: Model did not re-call tools for follow-up query!")
                return False
            
        except Exception as e:
            print(f"ERROR: {e}")
            # If error is due to MCP connection, allow the app to continue
            if "MCP" in str(e) or "connection" in str(e).lower():
                print("MCP connection issue detected. Allowing app to continue.")
                return True
            return False
    
    print("\nTool-calling reliability test PASSED!")
    return True


if __name__ == "__main__":
    # Test the agent
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Error: GROQ_API_KEY environment variable not set")
        print("Set it with: export GROQ_API_KEY=your_key_here")
    else:
        agent = create_agent(api_key)
        
        # Test tool calling reliability
        if test_tool_calling_reliability(agent):
            print("\nStarting interactive mode...")
            print("Type 'quit' to exit\n")
            
            while True:
                user_input = input("You: ")
                if user_input.lower() in ['quit', 'exit', 'q']:
                    break
                
                response = agent.process_message(user_input)
                print(f"Agent: {response.content}")
