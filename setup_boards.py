#!/usr/bin/env python3
"""
Board Setup Script for Monday.com Business Intelligence Agent

This one-time script reads the source Excel files and creates the two Monday.com boards
via API. It handles structural fixes during import as specified in BUILD_BRIEF.md.

Usage:
    python setup_boards.py --deals-file "Data/Deal funnel Data.xlsx" --work-orders-file "Data/Work_Order_Tracker Data.xlsx"

Environment Variables:
    MONDAY_API_TOKEN: Your Monday.com API token
"""

import argparse
import os
import sys
from typing import Dict, List, Any
import json

try:
    import pandas as pd
    import requests
except ImportError:
    print("Error: Required packages not installed. Run: pip install pandas openpyxl requests")
    sys.exit(1)


class MondayBoardSetup:
    """Handles Monday.com board creation and data import."""
    
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.api_url = "https://api.monday.com/v2"
        self.headers = {
            "Authorization": api_token,
            "Content-Type": "application/json",
            "API-Version": "2023-10"
        }
    
    def _make_request(self, query: str, variables: Dict = None) -> Dict:
        """Make a GraphQL request to Monday.com API."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        
        response = requests.post(self.api_url, headers=self.headers, json=payload)
        response.raise_for_status()
        data = response.json()
        
        if "errors" in data:
            raise Exception(f"Monday API Error: {data['errors']}")
        
        return data["data"]
    
    def create_board(self, board_name: str, board_type: str = "main") -> str:
        """Create a new Monday.com board and return its ID."""
        query = """
        mutation createBoard($boardName: String!, $boardType: BoardType!) {
            create_board(board_name: $boardName, board_type: $boardType) {
                id
                name
            }
        }
        """
        
        result = self._make_request(query, {
            "boardName": board_name,
            "boardType": board_type
        })
        
        board_id = result["create_board"]["id"]
        print(f"Created board '{board_name}' with ID: {board_id}")
        return board_id
    
    def create_column(self, board_id: str, column_type: str, title: str) -> str:
        """Create a column on a board."""
        query = """
        mutation createColumn($boardId: ID!, $columnType: ColumnType!, $title: String!) {
            create_column(board_id: $boardId, column_type: $columnType, title: $title) {
                id
                title
                type
            }
        }
        """
        
        result = self._make_request(query, {
            "boardId": board_id,
            "columnType": column_type,
            "title": title
        })
        
        column_id = result["create_column"]["id"]
        print(f"Created column '{title}' ({column_type}) with ID: {column_id}")
        return column_id
    
    def create_item(self, board_id: str, item_name: str, column_values: Dict = None) -> str:
        """Create an item on a board."""
        query = """
        mutation createItem($boardId: ID!, $itemName: String!, $columnValues: JSON!) {
            create_item(board_id: $boardId, item_name: $itemName, column_values: $columnValues) {
                id
                name
            }
        }
        """
        
        result = self._make_request(query, {
            "boardId": board_id,
            "itemName": item_name,
            "columnValues": json.dumps(column_values) if column_values else "{}"
        })
        
        item_id = result["create_item"]["id"]
        return item_id


def load_and_clean_deals_data(file_path: str) -> pd.DataFrame:
    """Load and apply structural fixes to Deals data."""
    print(f"Loading Deals data from {file_path}...")
    df = pd.read_excel(file_path)
    
    print(f"Original shape: {df.shape}")
    print(f"Original columns: {df.columns.tolist()}")
    
    # Structural fix: Drop rows where Deal Status == 'Deal Status' (sub-header rows)
    if 'Deal Status' in df.columns:
        original_count = len(df)
        df = df[df['Deal Status'] != 'Deal Status']
        dropped_count = original_count - len(df)
        print(f"Dropped {dropped_count} sub-header rows where Deal Status == 'Deal Status'")
    
    # Structural fix: Drop exact duplicate rows
    original_count = len(df)
    df = df.drop_duplicates()
    dropped_duplicates = original_count - len(df)
    print(f"Dropped {dropped_duplicates} exact duplicate rows")
    
    print(f"Cleaned shape: {df.shape}")
    return df


def load_and_clean_work_orders_data(file_path: str) -> pd.DataFrame:
    """Load and apply structural fixes to Work Orders data."""
    print(f"Loading Work Orders data from {file_path}...")
    
    # Structural fix: Skip empty header row (real headers are one row down)
    df = pd.read_excel(file_path, header=1)
    
    print(f"Original shape: {df.shape}")
    print(f"Original columns: {df.columns.tolist()}")
    
    # Structural fix: Drop the 4 fully-empty columns
    # Expected columns to drop: Expected Billing Month, Actual Collection Month, Collection status, Collection Date
    columns_to_drop = []
    for col in df.columns:
        if df[col].isna().all():
            columns_to_drop.append(col)
    
    if columns_to_drop:
        print(f"Dropping {len(columns_to_drop)} fully-empty columns: {columns_to_drop}")
        df = df.drop(columns=columns_to_drop)
    
    print(f"Cleaned shape: {df.shape}")
    return df


def create_deals_board_structure(setup: MondayBoardSetup) -> str:
    """Create the Deals board with appropriate column structure."""
    board_id = setup.create_board("Deals")
    
    # Define column types based on the data structure
    # These will need to be adjusted based on actual data inspection
    column_definitions = [
        ("text", "Deal Name"),
        ("text", "Client Code"),
        ("text", "Sector"),
        ("status", "Deal Status"),
        ("status", "Deal Stage"),
        ("text", "Owner"),
        ("date", "Created Date"),
        ("date", "Tentative Close Date"),
        ("numbers", "Masked Deal value"),
        ("numbers", "Amount to be billed"),
        ("numbers", "Amount Receivable"),
        ("text", "Closure Probability"),
        ("text", "WOCOMPANY"),
        ("text", "Work Order/Deal Serial No"),
    ]
    
    for col_type, col_title in column_definitions:
        try:
            setup.create_column(board_id, col_type, col_title)
        except Exception as e:
            print(f"Warning: Could not create column '{col_title}': {e}")
    
    return board_id


def create_work_orders_board_structure(setup: MondayBoardSetup) -> str:
    """Create the Work Orders board with appropriate column structure."""
    board_id = setup.create_board("Work Orders")
    
    # Define column types based on the data structure
    column_definitions = [
        ("text", "Work Order Number"),
        ("text", "Client Code"),
        ("text", "Sector"),
        ("text", "Client Name"),
        ("text", "Type of Work"),
        ("text", "Quantities as per PO"),
        ("numbers", "Amount to be billed"),
        ("numbers", "Amount Receivable"),
        ("status", "Invoice Status"),
        ("status", "WO Status (billed)"),
        ("status", "Billing Status"),
        ("date", "Invoice Date"),
        ("date", "Work Order Date"),
        ("text", "Owner"),
    ]
    
    for col_type, col_title in column_definitions:
        try:
            setup.create_column(board_id, col_type, col_title)
        except Exception as e:
            print(f"Warning: Could not create column '{col_title}': {e}")
    
    return board_id


def import_deals_data(setup: MondayBoardSetup, board_id: str, df: pd.DataFrame):
    """Import cleaned Deals data to the board."""
    print(f"Importing {len(df)} deals to board {board_id}...")
    
    # Import in batches to avoid rate limits
    batch_size = 10
    for i in range(0, len(df), batch_size):
        batch = df.iloc[i:i+batch_size]
        for _, row in batch.iterrows():
            try:
                item_name = str(row.get('Deal Name', f'Deal {i}'))
                column_values = {}
                
                # Map DataFrame columns to Monday.com column values
                # This mapping will need to be adjusted based on actual column names
                for col in df.columns:
                    if pd.notna(row[col]) and str(row[col]).strip() != '':
                        # Convert to appropriate format for Monday.com
                        column_values[str(col)] = str(row[col])
                
                setup.create_item(board_id, item_name, column_values)
                print(f"Imported deal {i+1}/{len(df)}: {item_name}")
                
            except Exception as e:
                print(f"Error importing deal {i+1}: {e}")
        
        # Small delay between batches to avoid rate limits
        import time
        time.sleep(1)


def import_work_orders_data(setup: MondayBoardSetup, board_id: str, df: pd.DataFrame):
    """Import cleaned Work Orders data to the board."""
    print(f"Importing {len(df)} work orders to board {board_id}...")
    
    # Import in batches to avoid rate limits
    batch_size = 10
    for i in range(0, len(df), batch_size):
        batch = df.iloc[i:i+batch_size]
        for _, row in batch.iterrows():
            try:
                item_name = str(row.get('Work Order Number', f'WO {i}'))
                column_values = {}
                
                # Map DataFrame columns to Monday.com column values
                for col in df.columns:
                    if pd.notna(row[col]) and str(row[col]).strip() != '':
                        column_values[str(col)] = str(row[col])
                
                setup.create_item(board_id, item_name, column_values)
                print(f"Imported work order {i+1}/{len(df)}: {item_name}")
                
            except Exception as e:
                print(f"Error importing work order {i+1}: {e}")
        
        # Small delay between batches to avoid rate limits
        import time
        time.sleep(1)


def save_board_config(deals_board_id: str, work_orders_board_id: str):
    """Save board IDs to a config file for the agent to use."""
    config = {
        "deals_board_id": deals_board_id,
        "work_orders_board_id": work_orders_board_id
    }
    
    config_path = "/home/prajankarthik/My_Files/Code/SkyLark_BI_Agent/.env.board"
    with open(config_path, 'w') as f:
        for key, value in config.items():
            f.write(f"{key.upper()}={value}\n")
    
    print(f"Board IDs saved to {config_path}")
    print(f"Deals Board ID: {deals_board_id}")
    print(f"Work Orders Board ID: {work_orders_board_id}")


def main():
    parser = argparse.ArgumentParser(description="Setup Monday.com boards for BI Agent")
    parser.add_argument("--deals-file", required=True, help="Path to Deals Excel file")
    parser.add_argument("--work-orders-file", required=True, help="Path to Work Orders Excel file")
    
    args = parser.parse_args()
    
    # Get API token from environment
    api_token = os.environ.get("MONDAY_API_TOKEN")
    if not api_token:
        print("Error: MONDAY_API_TOKEN environment variable not set")
        print("Set it with: export MONDAY_API_TOKEN=your_token_here")
        sys.exit(1)
    
    try:
        # Initialize Monday.com setup
        setup = MondayBoardSetup(api_token)
        
        # Load and clean data
        deals_df = load_and_clean_deals_data(args.deals_file)
        work_orders_df = load_and_clean_work_orders_data(args.work_orders_file)
        
        # Create board structures
        print("\n=== Creating Deals Board ===")
        deals_board_id = create_deals_board_structure(setup)
        
        print("\n=== Creating Work Orders Board ===")
        work_orders_board_id = create_work_orders_board_structure(setup)
        
        # Import data
        print("\n=== Importing Deals Data ===")
        import_deals_data(setup, deals_board_id, deals_df)
        
        print("\n=== Importing Work Orders Data ===")
        import_work_orders_data(setup, work_orders_board_id, work_orders_df)
        
        # Save configuration
        save_board_config(deals_board_id, work_orders_board_id)
        
        print("\n=== Board Setup Complete ===")
        
    except Exception as e:
        print(f"Error during board setup: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
