#!/bin/bash
# Test OpenResearcher + EHR integration with AWS Bedrock Claude

set -e

# Configuration
OUTPUT_DIR="./test_results"
MODEL="us.anthropic.claude-sonnet-4-5-v1:0"  # Bedrock model ID
SEARCH_URL="http://localhost:8001"  # Local search backend (or use Serper)
EHR_MCP_URL="http://127.0.0.1:5002/mcp"  # EHR MCP server
EHR_DATA_PATH="../data/EHRAgentBench"

mkdir -p $OUTPUT_DIR

echo "========================================="
echo "OpenResearcher + EHR Integration Test"
echo "========================================="
echo ""

# Check if EHR MCP server is running
echo "Checking EHR MCP server availability..."
if curl -s --connect-timeout 3 "$EHR_MCP_URL/health" > /dev/null 2>&1; then
    echo "✅ EHR MCP server is running at $EHR_MCP_URL"
else
    echo "⚠️  EHR MCP server not detected. Starting it now..."
    cd ..
    nohup python src/run_mcp_server.py \
        --mode http \
        --host 127.0.0.1 \
        --port 5002 \
        --data_path $EHR_DATA_PATH > $OUTPUT_DIR/ehr_server.log 2>&1 &
    EHR_PID=$!
    echo "Started EHR MCP server (PID: $EHR_PID)"
    sleep 5
    cd openresearcher_ehr
fi

echo ""
echo "Running test queries..."
echo ""

# Test 1: EHR-only query
echo "Test 1: EHR schema exploration"
python deploy_agent.py \
    --data_path test_queries_ehr.jsonl \
    --output_dir $OUTPUT_DIR/ehr_only \
    --model_name_or_path $MODEL \
    --use_bedrock \
    --bedrock_model_id $MODEL \
    --bedrock_region us-west-2 \
    --search_url $SEARCH_URL \
    --enable_ehr \
    --ehr_mcp_url $EHR_MCP_URL \
    --max_rounds 30 \
    --verbose

echo ""
echo "Test 2: Hybrid query (web + EHR)"
python deploy_agent.py \
    --data_path test_queries_hybrid.jsonl \
    --output_dir $OUTPUT_DIR/hybrid \
    --model_name_or_path $MODEL \
    --use_bedrock \
    --bedrock_model_id $MODEL \
    --bedrock_region us-west-2 \
    --search_url $SEARCH_URL \
    --enable_ehr \
    --ehr_mcp_url $EHR_MCP_URL \
    --max_rounds 50 \
    --verbose

echo ""
echo "========================================="
echo "Test completed!"
echo "========================================="
echo ""
echo "Results:"
echo "  - EHR only: $OUTPUT_DIR/ehr_only/results.jsonl"
echo "  - Hybrid:   $OUTPUT_DIR/hybrid/results.jsonl"
echo ""
echo "To view results:"
echo "  cat $OUTPUT_DIR/*/results.jsonl | jq ."
