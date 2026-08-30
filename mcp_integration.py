"""
MCP Integration for Monday.com Business Intelligence Agent

Connects to Monday.com's official MCP server as the read layer.
Per BUILD_BRIEF.md Section 3:
- Connect to Monday.com's official MCP server
- Auth via Monday.com API token from environment/config
- Handle connection failures gracefully
"""

import os
import json
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import asyncio
from contextlib import AsyncExitStack
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
except ImportError:
    print("Warning: MCP package not installed. Install with: pip install mcp")
    # Create dummy classes for development
    ClientSession = None
    create_mcp_http_client = None
    streamable_http_client = None


@dataclass
class MCPConfig:
    """Configuration for MCP connection."""
    api_token: str
    deals_board_id: str
    work_orders_board_id: str


class MondayMCPClient:
    """Read-only client for Monday.com's hosted Platform MCP server."""

    SERVER_URL = "https://mcp.monday.com/mcp"
    
    def __init__(self, config: MCPConfig):
        self.config = config
        self.session: Optional[ClientSession] = None
        self._connected = False
        self._exit_stack: Optional[AsyncExitStack] = None
    
    async def connect(self) -> bool:
        """
        Connect to Monday.com MCP server.
        Returns True if successful, False otherwise.
        """
        try:
            # Monday's Platform MCP is hosted and uses Streamable HTTP; there
            # is no local ``@modelcontextprotocol/server-monday`` npm package.
            # Keep the HTTP transport and session open through board reads.
            self._exit_stack = AsyncExitStack()
            http_client = await self._exit_stack.enter_async_context(
                create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self.config.api_token}"}
                )
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                streamable_http_client(self.SERVER_URL, http_client=http_client)
            )
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            
            # Initialize
            await self.session.initialize()
            
            self._connected = True
            return True
            
        except Exception as e:
            if self._exit_stack:
                await self._exit_stack.aclose()
                self._exit_stack = None
            self.session = None
            print(f"Failed to connect to Monday.com MCP server: {e}")
            self._connected = False
            return False
    
    async def disconnect(self):
        """Disconnect from MCP server."""
        try:
            if self._exit_stack:
                await self._exit_stack.aclose()
        except Exception as e:
            # Preserve the original connection/read error if the HTTP transport
            # also fails while being closed.
            print(f"Error closing Monday MCP connection: {e}")
        finally:
            self._exit_stack = None
            self.session = None
            self._connected = False
    
    async def list_available_tools(self) -> List[Dict[str, Any]]:
        """Return MCP tool definitions for a lightweight connection check."""
        if not self._connected or not self.session:
            raise RuntimeError("Not connected to MCP server")
        
        try:
            result = await self.session.list_tools()
            return result.tools if result else []
        except Exception as e:
            print(f"Error listing boards: {e}")
            return []
    
    async def get_board_items(self, board_id: str) -> List[Dict[str, Any]]:
        """
        Get all items from a specific board.
        """
        if not self._connected or not self.session:
            raise RuntimeError("Not connected to MCP server")
        
        try:
            columns = await self.get_board_columns(board_id)
            column_titles = {
                str(column.get("id")): column.get("title")
                for column in columns
                if isinstance(column, dict) and column.get("id") and column.get("title")
            }
            primary_column_title = next(
                (
                    column.get("title")
                    for column in columns
                    if isinstance(column, dict) and column.get("type") == "name"
                ),
                None,
            )
            items: List[Dict[str, Any]] = []
            cursor = None

            while True:
                arguments = {
                    "boardId": int(board_id),
                    "limit": 500,
                    "includeColumns": True,
                }
                if cursor:
                    arguments["cursor"] = cursor

                result = await self.session.call_tool("get_board_items_page", arguments)
                payload = self._extract_tool_payload(result)
                page_items = payload.get("items", []) if isinstance(payload, dict) else []
                if not isinstance(page_items, list):
                    raise RuntimeError("Monday MCP returned an invalid items payload")
                items.extend(
                    self._flatten_board_item(item, column_titles, primary_column_title)
                    for item in page_items
                    if isinstance(item, dict)
                )

                pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
                cursor = pagination.get("nextCursor") if isinstance(pagination, dict) else None
                if not pagination or not pagination.get("has_more") or not cursor:
                    return items
        except Exception as e:
            print(f"Error getting board items: {e}")
            raise RuntimeError(f"Unable to read Monday board {board_id}") from e
    
    async def get_board_columns(self, board_id: str) -> List[Dict[str, Any]]:
        """
        Get column schema for a specific board.
        """
        if not self._connected or not self.session:
            raise RuntimeError("Not connected to MCP server")
        
        try:
            result = await self.session.call_tool("get_board_info", {"boardId": int(board_id)})
            payload = self._extract_tool_payload(result)
            board_data = payload.get("board", {}) if isinstance(payload, dict) else {}
            return board_data.get("columns", []) if isinstance(board_data, dict) else []
        except Exception as e:
            print(f"Error getting board columns: {e}")
            raise RuntimeError(f"Unable to read schema for Monday board {board_id}") from e
    
    async def get_deals_data(self) -> List[Dict[str, Any]]:
        """Get all deals data from the Deals board."""
        return await self.get_board_items(self.config.deals_board_id)
    
    async def get_work_orders_data(self) -> List[Dict[str, Any]]:
        """Get all work orders data from the Work Orders board."""
        return await self.get_board_items(self.config.work_orders_board_id)
    
    async def get_deals_columns(self) -> List[Dict[str, Any]]:
        """Get column schema for Deals board."""
        return await self.get_board_columns(self.config.deals_board_id)
    
    async def get_work_orders_columns(self) -> List[Dict[str, Any]]:
        """Get column schema for Work Orders board."""
        return await self.get_board_columns(self.config.work_orders_board_id)
    
    def is_connected(self) -> bool:
        """Check if client is connected to MCP server."""
        return self._connected

    @staticmethod
    def _extract_tool_payload(result: Any) -> Any:
        """Decode structured or JSON-text MCP tool results without inventing data."""
        if not result:
            return {}

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        for content in getattr(result, "content", []):
            text = getattr(content, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return {}

    @staticmethod
    def _flatten_board_item(
        item: Dict[str, Any], column_titles: Dict[str, str], primary_column_title: Optional[str]
    ) -> Dict[str, Any]:
        """Map MCP column IDs to live board titles for the existing normalizer."""
        flattened = dict(item)
        column_values = item.get("column_values", item.get("columnValues", []))
        if isinstance(column_values, dict):
            for col_id, col_value in column_values.items():
                title = column_titles.get(str(col_id))
                if title:
                    # In dict format, col_value is directly the text/value
                    flattened[title] = col_value
        elif isinstance(column_values, list):
            for column_value in column_values:
                if not isinstance(column_value, dict):
                    continue
                title = column_value.get("title") or column_titles.get(str(column_value.get("id")))
                if title:
                    flattened[title] = column_value.get("text", column_value.get("value"))

        if primary_column_title and primary_column_title not in flattened:
            flattened[primary_column_title] = item.get("name")
        return flattened


def load_mcp_config() -> MCPConfig:
    """
    Load MCP configuration from environment variables.
    """
    api_token = os.environ.get("MONDAY_API_TOKEN")
    if not api_token:
        raise ValueError("MONDAY_API_TOKEN environment variable not set")
    
    deals_board_id = os.environ.get("DEALS_BOARD_ID")
    work_orders_board_id = os.environ.get("WORK_ORDERS_BOARD_ID")
    
    if not deals_board_id or not work_orders_board_id:
        # Try to load from board config file
        try:
            with open(".env.board", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEALS_BOARD_ID="):
                        deals_board_id = line.split("=", 1)[1]
                    elif line.startswith("WORK_ORDERS_BOARD_ID="):
                        work_orders_board_id = line.split("=", 1)[1]
        except FileNotFoundError:
            pass
    
    if not deals_board_id:
        raise ValueError("DEALS_BOARD_ID not set in environment or .env.board file")
    if not work_orders_board_id:
        raise ValueError("WORK_ORDERS_BOARD_ID not set in environment or .env.board file")
    
    return MCPConfig(
        api_token=api_token,
        deals_board_id=deals_board_id,
        work_orders_board_id=work_orders_board_id
    )


async def test_mcp_connection() -> bool:
    """
    Test MCP connection and return True if successful.
    """
    try:
        config = load_mcp_config()
        client = MondayMCPClient(config)
        
        success = await client.connect()
        if success:
            print("Successfully connected to Monday.com MCP server")
            
            tools = await client.list_available_tools()
            print(f"Found {len(tools)} MCP tools")
            
            await client.disconnect()
            return True
        else:
            print("Failed to connect to Monday.com MCP server")
            return False
            
    except Exception as e:
        print(f"Error testing MCP connection: {e}")
        return False


# Synchronous wrapper for use in non-async contexts
def sync_get_deals_data() -> List[Dict[str, Any]]:
    """Synchronous wrapper to get deals data."""
    async def _get():
        config = load_mcp_config()
        client = MondayMCPClient(config)
        try:
            if await client.connect():
                data = await client.get_deals_data()
                return data
            raise RuntimeError("Unable to connect to the Monday MCP server")
        finally:
            await client.disconnect()
    
    return asyncio.run(_get())


def sync_get_work_orders_data() -> List[Dict[str, Any]]:
    """Synchronous wrapper to get work orders data."""
    async def _get():
        config = load_mcp_config()
        client = MondayMCPClient(config)
        try:
            if await client.connect():
                data = await client.get_work_orders_data()
                return data
            raise RuntimeError("Unable to connect to the Monday MCP server")
        finally:
            await client.disconnect()
    
    return asyncio.run(_get())


def sync_get_board_schemas() -> Dict[str, List[Dict[str, Any]]]:
    """Synchronous wrapper to get board schemas."""
    async def _get():
        config = load_mcp_config()
        client = MondayMCPClient(config)
        try:
            if await client.connect():
                deals_cols = await client.get_deals_columns()
                wo_cols = await client.get_work_orders_columns()
                return {
                    "deals": deals_cols,
                    "work_orders": wo_cols
                }
            return {"deals": [], "work_orders": []}
        finally:
            await client.disconnect()
    
    return asyncio.run(_get())


if __name__ == "__main__":
    # Test MCP connection
    print("Testing Monday.com MCP connection...")
    success = asyncio.run(test_mcp_connection())
    if success:
        print("MCP connection test passed!")
    else:
        print("MCP connection test failed!")
