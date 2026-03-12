import os
import json
import pandas as pd
# 获取当前工作目录
current_directory = os.getcwd()
print("当前工作目录是:", current_directory)
import asyncio
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# 为 stdio 连接创建服务器参数
# transport = StdioTransport(
#         command="python",
#         args=["run_mcp_server.py",],
#         env=None,
#         # cwd="/path/to/server"
#     )
# mcp_client = Client(transport)
mcp_client = Client("http://192.168.169.2:5000/mcp")
# cp_client = Client("http://127.0.0.1:7000/mcp")

async def main():
    # 创建 stdio 客户端
    async with mcp_client:
        # 创建 ClientSession 对象
        # async with ClientSession(stdio, write) as session:
        #     # 初始化 ClientSession
        #     await session.initialize()

            # 列出可用的工具
        # response = await mcp_client.list_tools()
        # print(response)

            # 调用工具
        # get_table_names
        # response = await mcp_client.call_tool('get_table_names', {'subject_id': '11398418'})
        # print(response)
        # response = await mcp_client.call_tool('load_ehr', {'subject_id': '19494795', 'timestamp': '2155-03-29 16:59:00'})
        # print(response)
        # response = await mcp_client.call_tool('get_records_by_keyword', {'subject_id': '19494795', 'table_name': 'diagnoses_icd', 'keyword': 'heart failure'})
        # print(response)

        # "get_candidates_by_semantic_similarity",
        #         "params": "{'table_name': 'diagnoses_ccs_candidates', 'query': ['schizophrenia', 'NOS']}",
        # response = await mcp_client.call_tool('get_candidates_by_semantic_similarity', {'table_name': 'diagnoses_ccs_candidates', 'query': ['schizophrenia', 'NOS']})
        response = await mcp_client.list_tools()
        from pprint import pprint
        pprint(response)
        # response = await mcp_client.call_tool('get_candidates_by_semantic_similarity', {'table_name': 'diagnoses_ccs_candidates', 'query': 'infection'})
        
        # response = await mcp_client.call_tool('retrieve_pubmed', {'query': 'infection'})
        # print(response)

        # response = await mcp_client.call_tool('load_ehr', {'subject_id': '11398418', 'timestamp': '2133-04-04 11:10:00'})
        # print(response)

        # response = await mcp_client.call_tool('get_records_by_value', {'subject_id': '11398418', 'table_name': 'diagnoses_icd', 'column_name': 'subject_id', 'value': '11398418'})
        # print(response)

        # response = await mcp_client.call_tool('get_column_names', {'subject_id': '11398418', 'table_name': 'diagnoses_icd'})
        # print(response)

if __name__ == '__main__':
    asyncio.run(main())
