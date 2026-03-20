import json
import inspect
import pandas as pd
from typing import Optional
from fastmcp import Context

SESSION_EHR_DATA_KEY = "ehr_data_json"
SESSION_EHR_TABLE_NAMES_KEY = "ehr_table_names"
SESSION_EHR_SUBJECT_ID_KEY = "ehr_subject_id"
SESSION_EHR_TIMESTAMP_KEY = "ehr_timestamp"

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


def _get_ehr_session_store():
    """Get the process-global per-session EHR snapshot store."""
    import sys

    server_module = sys.modules.get('__main__')
    if server_module and hasattr(server_module, 'ehr_session_store'):
        return server_module.ehr_session_store
    return None


async def ctx_get_state(ctx: Context, key: str, default=None):
    """Read a state value from FastMCP context, handling sync/async APIs."""
    getter = getattr(ctx, "get_state", None)
    if getter is None:
        return default

    value = getter(key)
    if inspect.isawaitable(value):
        value = await value
    return default if value is None else value


async def ctx_set_state(ctx: Context, key: str, value) -> None:
    """Write a state value to FastMCP context, handling sync/async APIs."""
    setter = getattr(ctx, "set_state", None)
    if setter is None:
        return

    result = setter(key, value)
    if inspect.isawaitable(result):
        await result


def _parse_ehr_uri(uri: str):
    """Parse EHR resource URIs into (subject_id, table_name)."""
    prefix = "cache://ehr/ehr_data/"
    if not uri.startswith(prefix):
        return None, None

    suffix = uri[len(prefix):]
    if suffix.endswith("/table_list.json"):
        subject_id = suffix[: -len("/table_list.json")]
        return subject_id, None

    if not suffix.endswith(".json"):
        return None, None

    parts = suffix.split("/")
    if len(parts) < 2:
        return None, None
    subject_id = parts[-2]
    table_name = parts[-1].replace(".json", "")
    return subject_id, table_name


async def _get_session_ehr_data(ctx: Context):
    """Return session-scoped EHR data snapshot if available."""
    session_store = _get_ehr_session_store()
    if session_store is not None:
        session_state = session_store.get(ctx.session_id)
        if session_state is not None:
            return session_state.get(SESSION_EHR_DATA_KEY)

    return await ctx_get_state(ctx, SESSION_EHR_DATA_KEY, default=None)


async def _get_session_ehr_table_names(ctx: Context):
    """Return session-scoped EHR table names if available."""
    session_store = _get_ehr_session_store()
    if session_store is not None:
        session_state = session_store.get(ctx.session_id)
        if session_state is not None:
            return session_state.get(SESSION_EHR_TABLE_NAMES_KEY)

    return await ctx_get_state(ctx, SESSION_EHR_TABLE_NAMES_KEY, default=None)

async def get_resource_df(ctx: Context, uri: str) -> pd.DataFrame:
    """Get resource as DataFrame, preferring session-scoped EHR data when available."""
    subject_id, table_name = _parse_ehr_uri(uri)
    if subject_id and table_name:
        session_ehr_data = await _get_session_ehr_data(ctx)
        if session_ehr_data is not None:
            table_data = session_ehr_data.get(table_name)
            if table_data is None:
                return None
            return pd.DataFrame(table_data)

    # Try to read from context resources next
    try:
        blocks = await ctx.read_resource(uri)
        if blocks:
            blk = blocks[0]
            text = blk.content
            data = json.loads(text)
            return pd.DataFrame(data)
    except Exception:
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
    """Get resource data, preferring session-scoped EHR state when available."""
    subject_id, table_name = _parse_ehr_uri(uri)
    if subject_id:
        if table_name is None:
            session_table_names = await _get_session_ehr_table_names(ctx)
            if session_table_names is not None:
                return session_table_names
        else:
            session_ehr_data = await _get_session_ehr_data(ctx)
            if session_ehr_data is not None:
                return session_ehr_data.get(table_name)

    # Try to read from context resources next
    try:
        blocks = await ctx.read_resource(uri)
        if blocks:
            blk = blocks[0]
            text = blk.content
            data = json.loads(text)
            return data
    except Exception:
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
