import pandas as pd
from typing import Dict
import inspect

from typing import Annotated
from pydantic import Field
import sqlite3
from fastmcp import Context

from agentlite.commons.fastmcp import mcp
from agentlite.mcp_tools.tool_utils import find_timestamp_column, normalize_datetime, get_resource, get_resource_df

@mcp.tool(
    name="get_records_by_time",
    description="Finds records in a EHR Table that fall within a given time range. This is useful for getting detailed event data for a specific period. Not that this tool cannot be used to search from Candidate Tables.",
)
async def get_records_by_time(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'admissions', 'labevents').")], 
    start_time: Annotated[str, Field(description="The start of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format.")],
    end_time: Annotated[str, Field(description="The end of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format.")]
) -> str:
    """
    Retrieves event records from a table within a specified time range.
    
    Args:
        table_name (str): The name of the table to search in (e.g., 'admissions', 'labevents').
        start_time (str): The start of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format.
        end_time (str): The end of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format.
        
    Returns:
        str: A formatted string of the matching records, or an error message.
    """
    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    table_list = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if df is None:
        if candidate_tables and table_name in candidate_tables:
            function_name = inspect.currentframe().f_code.co_name
            return f"Error: The tool `{function_name}` cannot be used to search the Candidate Table: `{table_name}`."
        else:
            return f"Error: Table '{table_name}' not found in EHR Table list: {table_list}. Make sure to call load_ehr first."

    # 使用封装好的函数来找到时间戳列
    timestamp_column = find_timestamp_column(df)
    if timestamp_column is None:
        return f"Error: No timestamp column found in table '{table_name}'. Cannot filter by time."

    try:
        # 确保时间列是datetime类型
        df[timestamp_column] = pd.to_datetime(df[timestamp_column])
        
        # 转换开始和结束时间
        start_ts = normalize_datetime(start_time)
        end_ts = normalize_datetime(end_time)
        
        df[timestamp_column] = df[timestamp_column].apply(normalize_datetime)
        
        # 筛选出符合时间范围的记录
        filtered_df = df[(df[timestamp_column] >= start_ts) & (df[timestamp_column] <= end_ts)]
        
        if filtered_df.empty:
            return f"No records found in table '{table_name}' between {start_time} and {end_time}."
        
        return filtered_df.to_string(index=False)
        
    except Exception as e:
        return f"An error occurred while filtering records: {str(e)}"

@mcp.tool(
    name="get_event_counts_by_time",
    description="Calculates the number of events in all EHR Tables that fall within a given time range. This is useful for summarizing activity frequency.",
)
async def get_event_counts_by_time(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    start_time: Annotated[str, Field(description="The start of the time range (e.g., '2150-12-01', '2150-12-01 10:00').")],
    end_time: Annotated[str, Field(description="The end of the time range (e.g., '2150-12-02', '2150-12-02 23:59:59').")]
) -> str:
    """
    Retrieves event counts from all EHR Tables within a specified time range.
    
    Args:
        start_time (str): The start time string.
        end_time (str): The end time string.
        
    Returns:
        str: A formatted string of event counts, or an error message.
    """
    # 統一處理輸入的開始和結束時間
    start_ts = normalize_datetime(start_time)
    end_ts = normalize_datetime(end_time)

    if start_ts is None or end_ts is None:
        return "Error: Start time or end time format is invalid. Please use a format like 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."
        
    # 從 EHRManager 獲取所有 EHR 表的名稱
    ehr_table_names = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")

    if ehr_table_names is None:
        return "Error: No EHR data loaded. Please call load_ehr first."

    counts: Dict[str, int] = {}

    # 遍歷每個 EHR 表
    for table_name in ehr_table_names:
        df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
        if df is None:
            continue

        # 找到時間戳列
        timestamp_column = find_timestamp_column(df)
        if timestamp_column is None or timestamp_column not in df.columns:
            counts[table_name] = -1 # 用 -1 表示無法計數
            continue
        
        try:
            # 統一處理 DataFrame 中時間列的值
            df[timestamp_column] = df[timestamp_column].apply(normalize_datetime)
            
            # 篩選出符合時間範圍的記錄
            filtered_df = df[(df[timestamp_column] >= start_ts) & (df[timestamp_column] <= end_ts)].dropna(subset=[timestamp_column])
            
            counts[table_name] = len(filtered_df)
            
        except Exception as e:
            # 如果發生錯誤，將計數設為 -2 並記錄錯誤
            counts[table_name] = -2 
    
    # 格式化輸出
    output_lines = [f"Event counts between {start_time} and {end_time}:"]
    for table, count in counts.items():
        if count == -1:
            output_lines.append(f"- {table}: Could not find a timestamp column.")
        elif count == -2:
            output_lines.append(f"- {table}: An error occurred while processing this table.")
        else:
            output_lines.append(f"- {table}: {count} events")
    
    return "\n".join(output_lines)

@mcp.tool(
    name="get_latest_records",
    description="Finds the latest timestamp and returns all EHR Table that share that same timestamp in EHR Table. This is useful for getting the very lastest events, like the most recent lab results or prescriptions. Not that this tool cannot be used to search from Candidate Tables.",
)
async def get_latest_records(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to retrieve the latest records from (e.g., 'labevents', 'prescriptions').")]
) -> str:
    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    table_list = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if df is None:
        if candidate_tables and table_name in candidate_tables:
            function_name = inspect.currentframe().f_code.co_name
            return f"Error: The tool `{function_name}` cannot be used to search the Candidate Table: `{table_name}`."
        else:
            return f"Error: Table '{table_name}' not found in EHR Table list: {table_list}. Make sure to call load_ehr first."

    # 使用工具函数找到时间戳列
    timestamp_column = find_timestamp_column(df)
    if timestamp_column is None:
        return f"Error: No timestamp column found in table '{table_name}'. Cannot find the latest records."
    
    try:
        # 确保时间列是 datetime 类型
        df[timestamp_column] = pd.to_datetime(df[timestamp_column])
        
        # 找到最新的时间戳
        latest_timestamp = df[timestamp_column].max()
        if pd.isna(latest_timestamp):
            return f"No valid timestamps found in table '{table_name}' to determine the latest records."
            
        # 筛选出所有与最新时间戳匹配的记录
        latest_records = df[df[timestamp_column] == latest_timestamp]
        
        if latest_records.empty:
            return f"No records found in table '{table_name}' for the latest timestamp: {latest_timestamp}."

        return latest_records.to_string(index=False)
        
    except Exception as e:
        return f"An error occurred while retrieving the latest records: {str(e)}"

@mcp.tool(
    name="get_records_by_keyword",
    description="Searches for all text-based columns of the specific EHR Table containing a specific keyword. It is useful for finding the record without specific the column name. Not that this tool cannot be used to search from Candidate Tables.",
)
async def get_records_by_keyword(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'admissions', 'notes').")],
    keyword: Annotated[str, Field(description="The keyword to search for (e.g., 'pneumonia', 'fever').")]
) -> str:
    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    table_list = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if df is None:
        if candidate_tables and table_name in candidate_tables:
            function_name = inspect.currentframe().f_code.co_name
            return f"Error: The tool `{function_name}` cannot be used to search the Candidate Table: `{table_name}`."
        else:
            return f"Error: Table '{table_name}' not found in EHR Table list: {table_list}. Make sure to call load_ehr first."

    # 将关键词转换为小写，以进行不区分大小写的匹配
    search_keyword = keyword.lower()
    
    # 找出所有文本（object/string）类型的列
    text_cols = [col for col in df.columns if df[col].dtype in ['object', 'string']]
    
    if not text_cols:
        return f"Error: No text-based columns found in table '{table_name}' to search."
        
    # 构建一个布尔掩码，用于筛选包含关键词的行
    # 对每个文本列进行搜索，然后将结果用 OR 运算符连接
    mask = None
    for col in text_cols:
        # fillna('') 将 NaN 值替换为空字符串，以避免在 str.contains() 中出错
        col_mask = df[col].str.lower().str.contains(search_keyword, na=False)
        if mask is None:
            mask = col_mask
        else:
            mask = mask | col_mask
            
    # 应用掩码，获取匹配的记录
    matching_records = df[mask]
    
    if matching_records.empty:
        return f"No records found in table '{table_name}' containing the keyword '{keyword}'."
    
    # 返回匹配记录的格式化字符串
    return matching_records.to_string(index=False)

@mcp.tool(
    name="get_records_by_value",
    description="Finds records in a EHR Table where a given column's value is exact match for the keyword. This is useful for precise lookups using identifiers, codes, or specific categorical values. Not that this tool cannot be used to search from Candidate Tables.",
)
async def get_records_by_value(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'patients', 'd_icd_diagnoses').")],
    column_name: Annotated[str, Field(description="The name of the column to perform the search on (e.g., 'subject_id', 'itemid').")],
    value: Annotated[str, Field(description="The exact value to search.")]
) -> str:
    """
    Gets records in a table where a specified column's value matches the given value exactly.
    
    Args:
        table_name (str): The name of the table to search.
        column_name (str): The name of the column to search.
        value (str): The value to search for.
        
    Returns:
        str: A formatted string of the matching records, or a message indicating no matches were found.
    """
    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
    table_list = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
    candidate_tables = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if df is None:
        if candidate_tables and table_name in candidate_tables:
            function_name = inspect.currentframe().f_code.co_name
            return f"Error: The tool `{function_name}` cannot be used to search the Candidate Table: `{table_name}`."
        else:
            return f"Error: Table '{table_name}' not found in EHR Table list: {table_list}. Make sure to call load_ehr first."
        
    if column_name not in df.columns:
        return f"Error: Column '{column_name}' not found in table '{table_name}'."

    try:
        # 确保列是字符串类型，并处理可能存在的 NaN 值
        # 使用 .astype(str) 来强制转换为字符串，以确保精确匹配的正确性
        # 然后使用 .str.strip() 移除可能存在的首尾空格，进一步提高匹配的准确性
        # 最后使用 .fillna('') 来处理 NaN 值
        df[column_name] = df[column_name].astype(str).str.strip().fillna('')
        
        # 使用简单的布尔索引进行精确匹配
        matching_records = df[df[column_name] == str(value).strip()]

        if matching_records.empty:
            return f"No records found in table '{table_name}' where '{column_name}' equals '{value}'."
        
        # 返回匹配记录的格式化字符串
        return matching_records.to_string(index=False)
            
    except Exception as e:
        return f"An error occurred while searching for records: {str(e)}"
    
@mcp.tool(
    name="run_sql_query",
    description="Executes a standard SQL query against the patient's EHR Table. All available tables (e.g., 'labevents', 'admissions') are automatically loaded as SQL tables. Use this for complex filtering, joins, aggregations, or finding trends across multiple tables.",
)
async def run_sql_query(
    ctx: Context,
    subject_id: Annotated[str, Field(description="The unique identifier for the patient (e.g., '10000032').")],
    sql_query: Annotated[str, Field(description="A valid SQL query string. Table names match the EHR file names (e.g., SELECT * FROM labevents WHERE valuenum > 5).")]
) -> str:
    """
    Executes a SQL query on the patient's data using an in-memory SQLite database.
    
    Args:
        subject_id (str): The patient ID.
        sql_query (str): The SQL query to execute.
        
    Returns:
        str: The query result formatted as a string, or an error message.
    """
    # 1. 获取该患者所有可用的表名
    try:
        record_list = await get_resource(ctx, f"cache://ehr/ehr_data/{subject_id}/table_list.json")
        candidate_list = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")

        if not record_list:
            return f"Error: No tables found for subject_id {subject_id}. Please call load_ehr first."

        # Ensure candidate_list is a list (it might be None)
        candidate_list = candidate_list or []
        table_list = record_list + candidate_list
    except Exception as e:
        return f"Error retrieving table list: {str(e)}"

    # 2. 创建内存数据库连接
    # check_same_thread=False 允许在异步环境中使用
    conn = sqlite3.connect(':memory:', check_same_thread=False)

    try:
        tables_loaded = 0
        
        # 3. 将所有 DataFrame 加载到 SQL 中
        # 为了避免不必要的性能开销，我们只加载 SQL 语句中提到过的表？
        # 但简单的做法是全部加载，对于单患者数据量通常没问题。
        for table_name in table_list:
            # 简单的优化：只有当 SQL 语句中包含该表名时才加载 (大小写不敏感检查)
            if table_name.lower() in sql_query.lower():
                if table_name in record_list:
                    df = await get_resource_df(ctx, f"cache://ehr/ehr_data/{subject_id}/{table_name}.json")
                else:
                    df = await get_resource_df(ctx, f"cache://ehr/candidate_data/{table_name}.json")
                    
                if df is not None:
                    # 自动处理时间列，确保 SQLite 能进行简单的字符串比较或日期函数操作
                    # SQLite 默认没有 datetime 类型，通常存为字符串 ISO8601
                    for col in df.columns:
                        if 'time' in col.lower() or 'date' in col.lower():
                             df[col] = df[col].astype(str)
                    
                    # 写入 SQL 表，如果表名包含特殊字符可能需要处理，但通常 EHR 表名很规范
                    df.to_sql(table_name, conn, index=False, if_exists='replace')
                    tables_loaded += 1
        
        if tables_loaded == 0:
            return f"Error: The SQL query does not seem to reference any available tables. Available tables: {table_list}"

        # 4. 执行 SQL 查询
        result_df = pd.read_sql_query(sql_query, conn)
        
        if result_df.empty:
            return "Query executed successfully but returned no results."
            
        return result_df.to_string(index=False)

    except Exception as e:
        return f"SQL Execution Error: {str(e)}"
    finally:
        # 5. 关闭连接
        conn.close()