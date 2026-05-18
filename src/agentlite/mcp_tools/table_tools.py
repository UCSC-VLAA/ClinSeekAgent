import io
import pandas as pd
import numpy as np
import json
from typing import Dict, Any, Union, List
from thefuzz import fuzz

from typing import Annotated
from pydantic import Field
from fastmcp import Context

from agentlite.commons.fastmcp import mcp
from agentlite.mcp_tools.tool_utils import get_resource, get_resource_df

@mcp.tool(
    name="get_column_names",
    description="Retrieves all column names for a specified table. This is essential for understanding the data contained within a table and planning subsequent actions.",
)
async def get_column_names(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to retrieve column names from (e.g., 'admissions', 'd_icd_diagnoses').")]
) -> str:
    """
    Retrieves column names for a given table.
    
    Args:
        table_name (str): The name of the table.
        
    Returns:
        str: A formatted string of column names, or an error message.
    """
    # 從 EHRManager 的統一介面獲取表
    ehr_tables = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, "cache://ehr/candidate_data/table_list.json")

    if table_name in ehr_tables:
        df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    elif table_name in candidate_tables:
        df = await get_resource_df(ctx, f"cache://ehr/candidate_data/{table_name}.json")
    else:
        return f"Error: Table '{table_name}' not found in EHR or candidate table lists."
        
    columns = list(df.columns)
    
    return f"Columns for table '{table_name}': {', '.join(columns)}"
    
@mcp.tool(
    name="get_table_names",
    description="Retrieves the names of all available tables in the database, categorized into EHR tables and candidates tables. This action helps in understanding the overall data schema.",
)
async def get_table_names(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")]
) -> str:
    """
    Retrieves and categorizes all table names in the EHR database.
    
    Returns:
        str: A formatted string listing all available tables.
    """
    ehr_tables = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, "cache://ehr/candidate_data/table_list.json")
    
    output_lines = ["Available Tables:"]
    
    if ehr_tables:
        output_lines.append("\nEHR Tables:")
        output_lines.append(", ".join(sorted(ehr_tables)))
        
    if candidate_tables:
        output_lines.append("\nCandidate Tables:")
        output_lines.append(", ".join(sorted(candidate_tables)))
        
    if not ehr_tables and not candidate_tables:
        return "No tables are currently loaded or available."
    
    return "\n".join(output_lines)

@mcp.tool(
    name="get_table_description",
    description="Retrieve EHR table description and column information from the hospital database schema based on table name in a concise, readable format.",
)
async def get_table_description(
    ctx: Context,
    table_name: Annotated[str, Field(description="The specific table name within the category (e.g., 'admissions', 'labevents', 'patients')")]
) -> str:
    """
    Retrieve table description and column information from the hospital database schema.
    Args:
        table_name (str): The specific table name within the category
    Returns:
        str: Formatted table description in readable text format, or error message
    """
    discriptions = await get_resource(ctx, "cache://ehr/descriptions.json")
    if not discriptions:
        return "Error: Table descriptions resource not found."

    def format_table_description(table_info):
        """
        Format table information into a concise, readable format for LLM.
        """
        output_lines = []
        
        # Table header
        table_name = table_info.get("file_name", "Unknown")
        table_class = table_info.get("class", "")
        description = table_info.get("description", "")
        
        output_lines.append(f"Table: {table_name} ({table_class})")
        output_lines.append(f"Description: {description}")
        output_lines.append("Columns:")
        
        # Column information
        columns = table_info.get("columns", [])
        for col in columns:
            col_name = col.get("column_name", "")
            col_desc = col.get("description", "")
            output_lines.append(f"  - {col_name}: {col_desc}")
        
        return "\n".join(output_lines)

    try:
        # Parse each line as a separate JSON object
        for table_info in discriptions:
            try:
                # table_info = json.loads(line)
                if table_info.get("file_name") == table_name:
                    return format_table_description(table_info)
            except json.JSONDecodeError:
                continue
        
        # If table not found, return error message
        return f"Table '{table_name}' not found in category'"
        
    except Exception as e:
        return f"Error retrieving table description: {str(e)}"

@mcp.tool(
    name="get_unique_values",
    description="Retrieves all unique values from a specified categorical column in an EHR table. This is essential for understanding the specific vocabulary, codes, or flags used in the database (e.g., checking available options for 'admission_type' or 'flag').",
)
async def get_unique_values(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to retrieve values from (e.g., 'admissions').")],
    column_name: Annotated[str, Field(description="The name of the column to inspect (e.g., 'admission_type', 'flag').")]
) -> str:
    """
    Returns unique values from a specific column to help the agent understand categorical data.
    
    Args:
        table_name (str): Table name.
        column_name (str): Column name.
        
    Returns:
        str: A list of unique values or an error message.
    """
    # 1. 检查表是否存在
    ehr_tables = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    if table_name not in ehr_tables:
        return f"Error: Table '{table_name}' not found in EHR table list."

    # 2. 加载数据
    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    if df is None:
        return f"Error: Could not load table '{table_name}'."
        
    if column_name not in df.columns:
        return f"Error: Column '{column_name}' not found in table '{table_name}'."

    try:
        # 3. 获取唯一值
        # dropna() 去除空值，以免干扰
        unique_vals = df[column_name].dropna().unique()
        
        # 4. 安全性截断：如果唯一值太多，说明这可能不是一个分类列（可能是数值或ID），不应该全部返回
        limit = 50
        if len(unique_vals) > limit:
            return (
                f"Error: The column '{column_name}' has {len(unique_vals)} unique values, which is too many to display. "
                f"This tool is intended for categorical columns with fewer than {limit} options. "
                "Here are the first 10 examples: " + str(unique_vals[:10].tolist())
            )
        
        # 排序以方便阅读
        try:
            sorted_vals = sorted(unique_vals)
        except:
            # 如果类型混合无法排序，就保持原样
            sorted_vals = unique_vals
            
        return str(list(sorted_vals))

    except Exception as e:
        return f"An error occurred while retrieving unique values: {str(e)}"
