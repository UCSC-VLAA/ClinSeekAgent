# utils.py
import json
import pandas as pd
from typing import Optional
from fastmcp import Context

def normalize_datetime(dt_str: str) -> Optional[pd.Timestamp]:
    """
    Converts a date/time string to a standardized pandas Timestamp object.
    
    This function handles various formats and attempts to fill in missing components
    to ensure the timestamp is accurate to the second.
    
    Args:
        dt_str (str): The date/time string to normalize.
        
    Returns:
        Optional[pd.Timestamp]: A normalized Timestamp object, or None if conversion fails.
    """
    try:
        # 使用 pd.to_datetime 来处理多种格式
        timestamp = pd.to_datetime(dt_str)
        # 如果格式不完整，补全到秒
        if not timestamp.minute:
            timestamp = timestamp.replace(minute=0)
        if not timestamp.second:
            timestamp = timestamp.replace(second=0)
        return timestamp
    except (ValueError, TypeError):
        return None

def find_timestamp_column(df: pd.DataFrame) -> Optional[str]:
    """
    Finds the first column in a DataFrame whose name contains 'time' or 'date'.
    Returns the column name if found, otherwise returns None.
    """
    for col in df.columns:
        if 'time' in col.lower() or 'date' in col.lower():
            return col
    return None


def _get_ehr_manager():
    """Get the global EHRManager instance."""
    # Import here to avoid circular imports
    from agentlite.commons.fastmcp import mcp
    import sys
    # Get the run_mcp_server module from sys.modules
    server_module = sys.modules.get('__main__')
    if server_module and hasattr(server_module, 'ehr_manager'):
        return server_module.ehr_manager
    return None

async def get_resource_df(ctx: Context, uri: str) -> pd.DataFrame:
    """Get resource as DataFrame, falling back to global EHRManager if resource not found."""
    # Try to read from context resources first
    try:
        blocks = await ctx.read_resource(uri)
        if blocks:
            blk = blocks[0]
            text = blk.content
            data = json.loads(text)
            return pd.DataFrame(data)
    except:
        pass

    # Fall back to global EHRManager
    ehr_mgr = _get_ehr_manager()
    if ehr_mgr is None:
        return None

    # Parse URI to determine what data to get
    if "ehr_data/" in uri and ".json" in uri:
        parts = uri.split("/")
        if len(parts) >= 4:
            subject_id = parts[-2]
            table_name = parts[-1].replace(".json", "")
            if hasattr(ehr_mgr, 'ehr_data') and table_name in ehr_mgr.ehr_data:
                return ehr_mgr.ehr_data[table_name]
    elif "candidate_data/" in uri:
        parts = uri.split("/")
        table_name = parts[-1].replace(".json", "")
        if table_name in ehr_mgr.candidate_data:
            return ehr_mgr.candidate_data[table_name]

    return None

async def get_resource(ctx: Context, uri: str):
    """Get resource data, falling back to global EHRManager if resource not found."""
    # Try to read from context resources first
    try:
        blocks = await ctx.read_resource(uri)
        if blocks:
            blk = blocks[0]
            text = blk.content
            data = json.loads(text)
            return data
    except:
        pass

    # Fall back to global EHRManager
    ehr_mgr = _get_ehr_manager()
    if ehr_mgr is None:
        return None

    # Parse URI to determine what data to get
    if "table_list.json" in uri:
        if "ehr_data/" in uri:
            if hasattr(ehr_mgr, 'ehr_data'):
                return list(ehr_mgr.ehr_data.keys())
        elif "candidate_data/" in uri:
            return list(ehr_mgr.candidate_data.keys())
    elif "descriptions.json" in uri:
        return ehr_mgr.descriptions

    return None
