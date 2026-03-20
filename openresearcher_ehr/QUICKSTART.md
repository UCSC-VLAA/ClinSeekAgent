# Quick Start Guide

Get started with OpenResearcher + EHR integration in 5 minutes.

## Step 1: Prerequisites

```bash
# Ensure you're in the project directory
cd /fsx-shared/juncheng/EHR/openresearcher_ehr

# Install dependencies
pip install -r requirements.txt

# Configure AWS credentials (for Bedrock)
aws configure
# Enter your AWS Access Key ID, Secret, and set region to us-west-2
```

## Step 2: Start EHR MCP Server

In a separate terminal:

```bash
cd /fsx-shared/juncheng/EHR

# Start the EHR MCP server
python src/run_mcp_server.py \
    --mode http \
    --host 127.0.0.1 \
    --port 5003 \
    --data_path ./data/EHRAgentBench
```

Keep this running in the background.

## Step 3: Run Your First Query

### Example 1: Simple Web Search

```bash
# Create a test query
echo '{"qid": "test_001", "question": "What is diabetes mellitus?"}' > my_query.jsonl

# Run the agent
python deploy_agent.py \
    --data_path my_query.jsonl \
    --output_dir ./my_results \
    --verbose
```

### Example 2: EHR Query

```bash
# Create an EHR query
cat > ehr_query.jsonl <<EOF
{"qid": "ehr_001", "question": "Load patient 10000032's EHR at timestamp 2150-12-01 10:00:00, then list all available tables."}
EOF

# Run with EHR tools enabled
python deploy_agent.py \
    --data_path ehr_query.jsonl \
    --output_dir ./ehr_results \
    --enable_ehr \
    --ehr_mcp_url http://127.0.0.1:5003/mcp \
    --verbose
```

### Example 3: Hybrid Query (Web + EHR)

```bash
cat > hybrid_query.jsonl <<EOF
{"qid": "hybrid_001", "question": "Search the web for diabetes treatment guidelines, then search PubMed for recent clinical trials."}
EOF

python deploy_agent.py \
    --data_path hybrid_query.jsonl \
    --output_dir ./hybrid_results \
    --enable_ehr \
    --verbose
```

## Step 4: View Results

```bash
# View the results
cat ./my_results/results.jsonl | jq .

# Or view just the final answer
cat ./my_results/results.jsonl | jq -r '.messages[-1].content'
```

## Step 5: Run All Tests

```bash
# Run the comprehensive test suite
./test_integration.sh

# View test results
ls -lh test_results/
cat test_results/*/results.jsonl | jq .
```

## Common Tasks

### Add Your Own Queries

Create a JSONL file with your questions:

```jsonl
{"qid": "q1", "question": "Your question here"}
{"qid": "q2", "question": "Another question"}
```

### Use Different Models

```bash
# Use Claude Opus instead of Sonnet
python deploy_agent.py \
    --data_path my_query.jsonl \
    --model_name_or_path us.anthropic.claude-opus-4-6-v1:0 \
    --output_dir ./results
```

### Use Serper for Web Search

```bash
# Set your Serper API key
export SERPER_API_KEY="your_key_here"

# Run with Serper backend
python deploy_agent.py \
    --data_path my_query.jsonl \
    --browser_backend serper \
    --output_dir ./results
```

### Debug Issues

```bash
# Enable verbose mode
python deploy_agent.py \
    --data_path my_query.jsonl \
    --output_dir ./results \
    --verbose

# Check EHR server logs
cat ../test_results/ehr_server.log

# Reduce max rounds for faster debugging
python deploy_agent.py \
    --data_path my_query.jsonl \
    --max_rounds 10 \
    --verbose
```

## What's Next?

1. Read the full [README.md](README.md) for detailed documentation
2. Explore example queries in `test_queries_*.jsonl`
3. Check the [system architecture](#architecture) in README
4. Customize `data_utils.py` to add more tools
5. Modify `deploy_agent.py` for custom workflows

## Troubleshooting

**Issue: "Unable to locate credentials"**
```bash
# Solution: Configure AWS
aws configure
```

**Issue: "Connection refused" to EHR server**
```bash
# Solution: Start the EHR MCP server
cd /fsx-shared/juncheng/EHR
python src/run_mcp_server.py --mode http --port 5003 --data_path ./data/EHRAgentBench
```

**Issue: "Module not found: openai_harmony"**
```bash
# Solution: Install from OpenResearcher environment
cd /fsx-shared/juncheng/OpenResearcher
pip install -e .
```

**Issue: Browser search not working**
```bash
# Solution: Use Serper instead
export SERPER_API_KEY="your_key"
python deploy_agent.py --browser_backend serper ...
```

## Need Help?

- Check [README.md](README.md) for full documentation
- Review [test_integration.sh](test_integration.sh) for working examples
- Examine output in `./test_results/` for reference
- Check EHR MCP tools: `/fsx-shared/juncheng/EHR/src/agentlite/mcp_tools/`
