"""
Data utilities for ClinSeekAgent.
Combines browser tools with EHR clinical reasoning tools.
"""
import json
from pathlib import Path

# Task prompt templates (from original EHR system)
TASK_PROMPT_TEMPLATES = {
    "diagnoses_ccs": """<task_instruction>
Your current task is to act as a diagnostician.

Your objective is to determine all plausible diagnoses for the patient's current condition by analyzing the patient's complete history.

You must find the most likely official CCS candidates using the **`diagnoses_ccs_candidates`** reference table.

When you need medical knowledge or clinical information to support your diagnostic reasoning, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible diagnoses**. Each item in the list must be a string representing an official CCS diagnosis name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",

    "procedures_ccs": """<task_instruction>
Your current task is to act as a surgical planner.

Your objective is to determine all necessary surgical procedures for the patient by analyzing their complete medical history and established diagnoses.

You must find the most likely official CCS procedure candidates using the **`procedures_ccs_candidates`** reference table.

When you need medical knowledge or clinical information to support your procedure planning, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible procedures**. Each item in the list must be a string representing an official CCS procedure name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",

    "labevents": """<task_instruction>
Your current task is to act as a laboratory medicine specialist.

Your objective is to determine all necessary laboratory tests for the patient by analyzing their complete medical history, current clinical condition, and established diagnoses.

You should provide as many laboratory tests as possible to cover the patient's current clinical condition.

You must find the most likely official laboratory test candidates using the **`labevents_candidates`** reference table.

When you need medical knowledge or clinical information to support your laboratory planning, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible laboratory tests**. Each item in the list must be a string representing an official laboratory test name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",

    "prescriptions": """<task_instruction>
Your current task is to act as a pharmacist.

Your objective is to determine all necessary ATC therapeutic categories for the patient by analyzing their complete medical history, current clinical condition, and established diagnoses.

You must find the most likely official ATC name candidates using the **`prescriptions_atc_candidates`** reference data or semantic matching tools.

When you need medical knowledge or clinical information to support your medication planning, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible ATC names**. Each item in the list must be a string representing an official ATC name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",

    "microbiologyevents": """<task_instruction>
Your current task is to act as a clinical microbiologist.

Your objective is to determine all necessary microbiological tests for the patient by analyzing their complete medical history, current clinical condition, established diagnoses, and clinical signs of infection.

You must find the most likely official microbiological test candidates using the **`microbiologyevents_candidates`** reference data or semantic matching tools.

When you need medical knowledge or clinical information to support your microbiological assessment, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible microbiological tests**. Each item in the list must be a string representing an official microbiological test name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",

    "transfers": """<task_instruction>
Your current task is to act as a hospital care coordinator and clinical decision-maker.

Your objective is to determine the most appropriate care unit for patient transfer by analyzing their current clinical condition, medical history, severity of illness, and care requirements.

You must consider the patient's current location, clinical stability, required level of monitoring, and specialized care needs to recommend the optimal transfer destination.

You must find the most likely official care unit candidates using the **`transfers_candidates`** reference data or semantic matching tools.

When you need medical knowledge or clinical information to support your transfer planning, use the `browser.search` tool to find authoritative medical information from reliable sources.

Submit your final answer through `ehr.finish` as a **list** containing **multiple plausible care units**. Each item in the list must be a string representing an official care unit name, and **must not contain any codes or other additional information**.
</task_instruction>

<patient_info>
Current Time: {current_time}
Patient Subject ID: {subject_id}
</patient_info>""",
}

EHR_Bench_Prompt = """<task_instruction>
{instruction}

Analyze the patient's EHR records up to the current time to make your prediction.
Do not assume any unseen future events beyond the current time.
When you need medical knowledge or clinical information, use the `browser.search` tool to find authoritative medical information from reliable sources.
Submit your final answer through `ehr.finish`. Your answer must be chosen only from the candidate list below.
</task_instruction>

<patient_info>
Current Time: {prediction_time}
Patient Subject ID: {subject_id}
</patient_info>

<candidate_answers>
{candidates}
</candidate_answers>"""


def generate_ehr_bench_prompt(task_data):
    """
    Generate prompt for EHR-Bench tasks (both decision_making and risk_prediction).

    Args:
        task_data: dict from ehr_bench_sampled_20_per_task.json or ehr_bench_merged_filtered.json

    Returns:
        str: formatted prompt
    """
    candidates = task_data.get("candidates", [])
    if isinstance(candidates, list):
        candidates_str = json.dumps(candidates)
    else:
        candidates_str = str(candidates)

    return EHR_Bench_Prompt.format(
        instruction=task_data["instruction"],
        prediction_time=task_data["prediction_time"],
        subject_id=task_data["subject_id"],
        candidates=candidates_str,
    )

def generate_question_from_task(task_data):
    """
    Generate question prompt from task data based on task type.

    Args:
        task_data: dict with keys: subject_id, prediction_time, task, ground_truth

    Returns:
        str: formatted question prompt
    """
    task_type = task_data.get("task", "diagnoses_ccs")
    subject_id = task_data["subject_id"]
    current_time = task_data["prediction_time"]

    # Get template for task type, default to diagnoses_ccs
    template = TASK_PROMPT_TEMPLATES.get(task_type, TASK_PROMPT_TEMPLATES["diagnoses_ccs"])

    # Format with patient info
    question = template.format(current_time=current_time, subject_id=subject_id)

    return question

DEVELOPER_CONTENT_CLAUDE = """
You are a research assistant with access to both web browsing and clinical EHR tools.

**Browser Tools** (for web research and medical knowledge):
- browser.search: Search the web for information, medical knowledge, clinical guidelines, diagnostic criteria
- browser.open: Open and read web pages
- browser.find: Find text within pages

**EHR Tools** (for clinical data analysis):
- ehr.load_ehr: Load patient EHR database (must be called first for clinical tasks)
- ehr.get_table_names: List available patient data tables
- ehr.get_column_names: Get table column information
- ehr.get_records_by_time: Query patient records within time range
- ehr.run_sql_query: Execute SQL queries on patient database
- ehr.get_candidates_by_semantic_similarity: Search medical terminology/diagnosis codes
- ehr.get_candidates_by_keyword: Search diagnosis codes by keyword
- ehr.think: Record your reasoning process
- ehr.finish: Submit your final answer

**Important:** When you engage in thinking, reasoning, or analysis, you can use Browser Tools to support your process, including assisting with information retrieval and verification.

The `cursor` appears in brackets before each browsing display: `[{cursor}]`.
Cite web sources using: 【{cursor}†L{line_start}(-L{line_end})?】

sources=web,ehr
"""

SFT_MODEL_PROMPT = """
You are a research assistant with access to both web browsing and clinical EHR tools.

**Browser Tools** (for web research and medical knowledge):
- browser.search: Search the web for information, medical knowledge, clinical guidelines, diagnostic criteria
- browser.open: Open and read web pages
- browser.find: Find text within pages

**EHR Tools** (for clinical data analysis):
- ehr.load_ehr: Load patient EHR database (must be called first for clinical tasks)
- ehr.get_table_names: List available patient data tables
- ehr.get_column_names: Get table column information
- ehr.get_records_by_time: Query patient records within time range
- ehr.run_sql_query: Execute SQL queries on patient database
- ehr.get_candidates_by_semantic_similarity: Search medical terminology/diagnosis codes
- ehr.get_candidates_by_keyword: Search diagnosis codes by keyword
- ehr.think: Record your reasoning process
- ehr.finish: Submit your final answer

**Important:** When you engage in thinking, reasoning, or analysis, you can use Browser Tools to support your process, including assisting with information retrieval and verification.

**Tool Call Format Requirement:** Whenever you call a tool, you MUST emit the tool call in exactly this plain-text format:
`[Tool Call: {function_name}({arguments})]`

Formatting rules for tool calls:
- Use exactly the prefix `[Tool Call:`
- `function_name` must be the full tool name such as `ehr.load_ehr`, `ehr.get_records_by_time`, `browser.search`, or `ehr.finish`
- `{arguments}` must be a valid JSON object
- Do not use XML tool-call formats
- Do not use raw JSON arrays or other wrapper formats for tool calls
- If you need to call multiple tools in one response, emit one `[Tool Call: ...]` entry per tool
- When you have enough information to answer, you must call `ehr.finish` using this same format

The `cursor` appears in brackets before each browsing display: `[{cursor}]`.
Cite web sources using: 【{cursor}†L{line_start}(-L{line_end})?】

Your final response should be submitted by calling `ehr.finish` in the required `[Tool Call: ...]` format.

sources=web,ehr
"""
# Tool schema source-of-truth lives on disk so the MCP server side
# (`src/agentlite/mcp_tools/all_ehr_tools.json`) and this agent-side
# package stay in sync. `browser_tools.json` is client-only.
BROWSER_TOOL_CONTENT = (Path(__file__).resolve().parent / "browser_tools.json").read_text()
EHR_TOOL_CONTENT_JSON = (
    Path(__file__).resolve().parents[1]
    / "src" / "agentlite" / "mcp_tools" / "all_ehr_tools.json"
).read_text()


def get_combined_tools_with_all_ehr():
    """EHR tool list followed by the 3 browser tools (knowledge retrieval is delegated to web search)."""
    browser_tools = json.loads(BROWSER_TOOL_CONTENT)
    ehr_tools = json.loads(EHR_TOOL_CONTENT_JSON)
    return ehr_tools + browser_tools

# Default tool registry consumed by run_text.py (browser + full EHR tool set)
COMBINED_TOOL_CONTENT_FULL = json.dumps(get_combined_tools_with_all_ehr())
