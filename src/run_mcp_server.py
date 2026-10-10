import os
import asyncio
import pandas as pd
import numpy as np
import json
from pathlib import Path
from pydantic import Field
from typing import Annotated, List, Optional
from fastmcp import Context
from fastmcp.resources import TextResource
from agentlite.commons import EHRManager
from agentlite.commons.fastmcp import mcp
from agentlite.mcp_tools.tool_utils import (
    SESSION_EHR_DATA_KEY,
    SESSION_EHR_TABLE_NAMES_KEY,
    SESSION_EHR_SUBJECT_ID_KEY,
    SESSION_EHR_TIMESTAMP_KEY,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = Path("../data/clinseek_bench/ehr")


def get_parser():
    import argparse
    parser = argparse.ArgumentParser(description="EHR MCP Tool Server")
    parser.add_argument('--data_path', type=str, default=str(DEFAULT_DATA_PATH), help='Path to the EHR data directory')
    parser.add_argument('--mode', type=str, default="studio", choices=["studio", "http"], help='Mode to run the server in')
    parser.add_argument('--host', type=str, default="127.0.0.1", help='Host IP for HTTP mode')
    parser.add_argument('--port', type=int, default=9000, help='Port for HTTP mode')
    parser.add_argument(
        '--disable-knowledge-tools',
        action='store_true',
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--enable-knowledge-tools',
        action='store_true',
        help='Register optional corpus retrieval tools from knowledge_tools.py.',
    )
    return parser.parse_args()

args = get_parser()
if not os.path.isabs(args.data_path):
    args.data_path = str((SCRIPT_DIR / args.data_path).resolve())

ehr_manager = EHRManager(args.data_path)
load_ehr_lock = asyncio.Lock()
ehr_session_store = {}

@mcp.resource("cache://ehr/ehr_data/{subject_id}/{table_name}.json")
async def get_ehr_table_resource(subject_id: str, table_name: str):
    """Dynamic resource for EHR table data."""
    if not hasattr(ehr_manager, 'ehr_data') or not ehr_manager.ehr_data:
        return TextResource(uri=f"cache://ehr/ehr_data/{subject_id}/{table_name}.json", text=json.dumps({}), mime_type="application/json")

    table_data = ehr_manager.ehr_data.get(table_name)
    if table_data is None:
        return TextResource(uri=f"cache://ehr/ehr_data/{subject_id}/{table_name}.json", text=json.dumps({}), mime_type="application/json")

    df = table_data.astype(str)
    return TextResource(uri=f"cache://ehr/ehr_data/{subject_id}/{table_name}.json", text=json.dumps(df.to_dict(orient='list')), mime_type="application/json")

@mcp.resource("cache://ehr/ehr_data/{subject_id}/table_list.json")
async def get_ehr_table_list_resource(subject_id: str):
    """Dynamic resource for EHR table list."""
    if not hasattr(ehr_manager, 'ehr_data') or not ehr_manager.ehr_data:
        return TextResource(uri=f"cache://ehr/ehr_data/{subject_id}/table_list.json", text=json.dumps([]), mime_type="application/json")
    return TextResource(uri=f"cache://ehr/ehr_data/{subject_id}/table_list.json", text=json.dumps(list(ehr_manager.ehr_data.keys())), mime_type="application/json")

@mcp.resource("cache://ehr/candidate_data/{table_name}.json")
async def get_candidate_table_resource(table_name: str):
    """Dynamic resource for candidate table data."""
    table_data = ehr_manager.candidate_data.get(table_name)
    if table_data is None:
        return TextResource(uri=f"cache://ehr/candidate_data/{table_name}.json", text=json.dumps({}), mime_type="application/json")

    df = table_data.astype(str)
    return TextResource(uri=f"cache://ehr/candidate_data/{table_name}.json", text=json.dumps(df.to_dict(orient='list')), mime_type="application/json")

@mcp.resource("cache://ehr/candidate_data/table_list.json")
async def get_candidate_table_list_resource():
    """Dynamic resource for candidate table list."""
    return TextResource(uri=f"cache://ehr/candidate_data/table_list.json", text=json.dumps(list(ehr_manager.candidate_data.keys())), mime_type="application/json")

@mcp.resource("cache://ehr/descriptions.json")
async def get_descriptions_resource():
    """Dynamic resource for table descriptions."""
    return TextResource(uri=f"cache://ehr/descriptions.json", text=json.dumps(ehr_manager.descriptions), mime_type="application/json")

@mcp.tool(
    name="load_ehr",
    description="Load the ehr data for the given subject_id and current_timestamp. This action should be taken once at the beginning of each task.",
)
async def load_ehr(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient whose EHR database needs to be loaded (e.g., '10000032').")],
    timestamp: Annotated[str, Field(description="The current timestamp in 'YYYY-MM-DD HH:MM:SS' format (e.g., '2150-12-01 10:00:00').")],
    linked_study_ids: Optional[List[str]] = None,
    scope: Optional[str] = None,
    task: Optional[str] = None,
    hadm_id: Optional[str] = None,
    target_hadm_id: Optional[str] = None,
    target_stay_id: Optional[str] = None,
    modalities: Optional[List[str]] = None,
) -> str:
    """
    Loads the EHR database for a specific patient by subject ID.
    Args:
        subject_id (str): The unique identifier for the patient.
        timestamp (str): The current timestamp in 'YYYY-MM-DD HH:MM:SS' format.
    Returns:
        str: Success or error message.
    """
    try:
        # EHRManager keeps patient tables in a mutable process-global dict, so
        # concurrent load_ehr calls must bind and snapshot atomically before
        # handing the data off to session-local state.
        async with load_ehr_lock:
            existing = ehr_session_store.get(ctx.session_id)
            if existing is not None:
                existing_subject = str(
                    existing.get(SESSION_EHR_SUBJECT_ID_KEY, "")
                )
                existing_timestamp = pd.Timestamp(
                    existing.get(
                        "requested_prediction_time",
                        existing.get(SESSION_EHR_TIMESTAMP_KEY),
                    )
                )
                requested_timestamp = pd.Timestamp(timestamp)
                existing_task = existing.get("trusted_task")
                requested_task = str(task) if task is not None else None
                existing_hadm_id = existing.get("trusted_hadm_id")
                requested_hadm_id = (
                    str(hadm_id) if hadm_id not in (None, "") else None
                )
                existing_target_hadm_id = existing.get(
                    "trusted_target_hadm_id"
                )
                requested_target_hadm_id = (
                    str(target_hadm_id)
                    if target_hadm_id not in (None, "")
                    else None
                )
                existing_target_stay_id = existing.get(
                    "trusted_target_stay_id"
                )
                requested_target_stay_id = (
                    str(target_stay_id)
                    if target_stay_id not in (None, "")
                    else None
                )
                if (
                    str(subject_id) != existing_subject
                    or requested_timestamp != existing_timestamp
                    or requested_task != existing_task
                    or requested_hadm_id != existing_hadm_id
                    or requested_target_hadm_id
                    != existing_target_hadm_id
                    or requested_target_stay_id
                    != existing_target_stay_id
                ):
                    return (
                        "Error: this MCP session is already bound to a "
                        "different patient, prediction cutoff, task, or "
                        "index/target encounter."
                    )
                return (
                    "EHR snapshot already loaded for this session under "
                    "temporal policy "
                    f"{existing.get('temporal_policy_version', 'unknown')}."
                )

            ehr_manager.load_ehr_for_sample(
                subject_id,
                timestamp,
                linked_study_ids=linked_study_ids,
                scope=scope,
                task=task,
                hadm_id=hadm_id,
                target_hadm_id=target_hadm_id,
                target_stay_id=target_stay_id,
                modalities=modalities,
            )
            all_session_ehr_data = ehr_manager.get_ehr_data_json()
            # Do not reveal the existence of a patient table that has no
            # pre-cutoff rows. In per-patient databases, table presence itself
            # can otherwise reveal a future event category.
            session_table_names = [
                table_name
                for table_name, frame in ehr_manager.ehr_data.items()
                if not frame.empty
            ]
            session_ehr_data = {
                table_name: all_session_ehr_data[table_name]
                for table_name in session_table_names
            }
            temporal_policy_version = ehr_manager.temporal_policy_version
            temporal_report = ehr_manager.last_temporal_report

        ehr_session_store[ctx.session_id] = {
            SESSION_EHR_SUBJECT_ID_KEY: subject_id,
            # Time-aware query tools must use the effective runtime boundary.
            # For readmission this is the real index dischtime + 1 second,
            # not the source note's midnight charttime + 1 second.
            SESSION_EHR_TIMESTAMP_KEY: str(
                pd.Timestamp(ehr_manager.last_prediction_time)
            ),
            "requested_prediction_time": str(pd.Timestamp(timestamp)),
            "trusted_task": str(task) if task is not None else None,
            "trusted_hadm_id": (
                str(hadm_id) if hadm_id not in (None, "") else None
            ),
            "trusted_target_hadm_id": (
                str(target_hadm_id)
                if target_hadm_id not in (None, "")
                else None
            ),
            "trusted_target_stay_id": (
                str(target_stay_id)
                if target_stay_id not in (None, "")
                else None
            ),
            SESSION_EHR_DATA_KEY: session_ehr_data,
            SESSION_EHR_TABLE_NAMES_KEY: session_table_names,
            "temporal_policy_version": temporal_policy_version,
            # Server-side audit evidence. Never include row/drop/redaction
            # counts in the model-visible load_ehr observation.
            "temporal_report": temporal_report,
        }
        return (
            "EHR snapshot loaded successfully under temporal policy "
            f"{temporal_policy_version}."
        )
    except Exception as e:
        return f"An error occurred while loading EHR database: {str(e)}"


@mcp.tool(
    name="clear_session_ehr",
    description="Clear the currently loaded session-scoped EHR snapshot for the active MCP session.",
)
async def clear_session_ehr(ctx: Context) -> str:
    """Clear the session-local EHR snapshot for the current MCP session."""
    removed = ehr_session_store.pop(ctx.session_id, None)
    if removed is None:
        return "No session EHR snapshot found."
    return "Session EHR snapshot cleared."
        
# import agentlite.action_tools.mcp_tools
import agentlite.mcp_tools.table_tools
import agentlite.mcp_tools.record_tools
import agentlite.mcp_tools.candidate_tools
import agentlite.mcp_tools.resource_tools
import agentlite.mcp_tools.inner_tools

if args.enable_knowledge_tools and not args.disable_knowledge_tools:
    import agentlite.mcp_tools.knowledge_tools


async def run_mcp_server():
    if args.mode == "studio":
        mcp.run()
    else:
        await mcp.run_async(
            transport="http", 
            host=args.host,
            port=args.port,
        )

if __name__ == '__main__':

    asyncio.run(run_mcp_server())
