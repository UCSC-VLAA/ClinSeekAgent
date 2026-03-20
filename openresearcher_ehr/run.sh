export SERPER_API_KEY=61877f4a59d2968ae439a7d13d49dc2990bc0a1b
EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-10}

python deploy_agent.py \
    --data_path /home/efs/zlt/deepresearch/data/EHRAgentBench/common/diagnoses_ccs_500.json \
    --output_dir ./diagnoses_ccs_500_results \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --verbose
