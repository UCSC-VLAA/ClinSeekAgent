# OpenResearcher + EHR Integration

This directory contains an integrated system combining **OpenResearcher's web browsing capabilities** with **EHR clinical reasoning tools**. The system uses AWS Bedrock Claude as the reasoning engine to perform tasks that require both web information and clinical database access.

## Architecture

```
User Query
    ↓
Bedrock Claude (Sonnet 4.5)
    ↓
Tool Router → [Browser Tools] OR [EHR Tools]
    ↓                              ↓
BrowserPool                   EHRToolPool
    ↓                              ↓
Web Search/Scrape         MCP Server → Patient EHR DB
```

### Components

- **deploy_agent.py**: Main orchestrator that manages conversation flow and tool routing
- **data_utils.py**: Tool schemas and system prompts for browser + EHR tools
- **browser.py**: Browser tool implementation (search, open, find)
- **ehr_pool.py**: EHR tool pool that connects to MCP server via HTTP

### Tool Categories

**Browser Tools** (3 tools):
- `browser.search`: Search the web
- `browser.open`: Open and read web pages
- `browser.find`: Find text within pages

**EHR Tools** (7 tools):
- `ehr.load_ehr`: Load patient EHR database
- `ehr.get_table_names`: List available tables
- `ehr.get_column_names`: Get table structure
- `ehr.get_records_by_time`: Query records by time range
- `ehr.run_sql_query`: Execute SQL on patient data
- `ehr.get_candidates_by_semantic_similarity`: Search medical codes
- `ehr.retrieve_pubmed`: Search PubMed literature

## Prerequisites

### 1. Dependencies

```bash
# Install required Python packages
pip install httpx asyncio
pip install openai-harmony  # For browser tools
pip install gpt-oss         # For browser processing

# AWS credentials for Bedrock
export AWS_REGION=us-west-2
export AWS_PROFILE=default  # Or configure via ~/.aws/credentials
```

### 2. EHR MCP Server

The EHR MCP server must be running before starting the agent:

```bash
# Start in HTTP mode
cd /fsx-shared/juncheng/EHR
python src/run_mcp_server.py \
    --mode http \
    --host 127.0.0.1 \
    --port 5002 \
    --data_path ./data/EHRAgentBench
```

### 3. Search Backend (Optional)

For web search, you can use either:

**Option A: Local search backend** (if available)
```bash
# Assumes a search service running at http://localhost:8001
```

**Option B: Serper API** (cloud-based)
```bash
export SERPER_API_KEY="your_api_key"
# Then use --browser_backend serper flag
```

## Usage

### Basic Usage

```bash
python deploy_agent.py \
    --data_path test_queries_hybrid.jsonl \
    --output_dir ./results \
    --enable_ehr \
    --ehr_mcp_url http://127.0.0.1:5002/mcp \
    --verbose
```

### Command-Line Arguments

**Model Configuration:**
- `--model_name_or_path`: Bedrock model ID (default: `us.anthropic.claude-sonnet-4-5-v1:0`)
- `--use_bedrock`: Use AWS Bedrock (default: True)
- `--bedrock_model_id`: Override model ID
- `--bedrock_region`: AWS region (default: us-west-2)

**Browser Configuration:**
- `--search_url`: Search backend URL (default: http://localhost:8001)
- `--browser_backend`: Backend type: `local` or `serper` (default: local)

**EHR Configuration:**
- `--enable_ehr`: Enable EHR tools (required for clinical queries)
- `--ehr_mcp_url`: MCP server URL (default: http://127.0.0.1:5002/mcp)

**Data Configuration:**
- `--data_path`: Path to JSONL file with questions (required)
- `--output_dir`: Output directory for results (default: ./results)

**Execution Configuration:**
- `--max_rounds`: Max conversation rounds (default: 200)
- `--verbose`: Enable verbose logging

## Test Scripts

### Quick Test

```bash
# Make script executable
chmod +x test_integration.sh

# Run all tests
./test_integration.sh
```

This will:
1. Check if EHR MCP server is running (start if needed)
2. Run EHR-only queries
3. Run hybrid (web + EHR) queries
4. Save results to `./test_results/`

### Individual Tests

**Test 1: EHR schema exploration**
```bash
python deploy_agent.py \
    --data_path test_queries_ehr.jsonl \
    --output_dir ./results/ehr_test \
    --enable_ehr \
    --verbose
```

**Test 2: Web search only**
```bash
python deploy_agent.py \
    --data_path test_queries_web.jsonl \
    --output_dir ./results/web_test \
    --verbose
```

**Test 3: Hybrid (web + clinical)**
```bash
python deploy_agent.py \
    --data_path test_queries_hybrid.jsonl \
    --output_dir ./results/hybrid_test \
    --enable_ehr \
    --verbose
```

## Input Format

Input queries should be in JSONL format:

```jsonl
{"qid": "query_001", "question": "What are typical diabetes lab values?"}
{"qid": "query_002", "question": "Load patient 10000032's EHR at 2150-12-01..."}
```

## Output Format

Results are saved as JSONL with the following structure:

```json
{
  "qid": "query_001",
  "question": "What are typical diabetes lab values?",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."}
  ],
  "status": "success"
}
```

## Example Queries

### EHR-Only Query
```json
{
  "qid": "ehr_001",
  "question": "Load patient 10000032's EHR at timestamp 2150-12-01 10:00:00, then list all available tables and describe the diagnoses_icd table structure."
}
```

**Expected Tool Calls:**
1. `ehr.load_ehr(subject_id="10000032", timestamp="2150-12-01 10:00:00")`
2. `ehr.get_table_names(subject_id="10000032")`
3. `ehr.get_column_names(subject_id="10000032", table_name="diagnoses_icd")`

### Hybrid Query
```json
{
  "qid": "hybrid_001",
  "question": "What are typical lab values for diagnosing diabetes? Search web sources and verify against PubMed medical literature."
}
```

**Expected Tool Calls:**
1. `browser.search(query="diabetes diagnosis lab values clinical guidelines")`
2. `browser.open(id=1)` (open top result)
3. `ehr.retrieve_pubmed(query="diabetes mellitus diagnostic criteria")`

### Web-Only Query
```json
{
  "qid": "web_001",
  "question": "What are the current CDC guidelines for COVID-19 vaccination?"
}
```

**Expected Tool Calls:**
1. `browser.search(query="CDC COVID-19 vaccination guidelines 2026")`
2. `browser.open(id=<CDC_URL>)`
3. `browser.find(pattern="booster dose")`

## Troubleshooting

### EHR MCP Server Not Available
```
Error: HTTP Error 404: Not Found
```
**Solution:** Start the EHR MCP server:
```bash
cd /fsx-shared/juncheng/EHR
python src/run_mcp_server.py --mode http --host 127.0.0.1 --port 5002 --data_path ./data/EHRAgentBench
```

### AWS Credentials Error
```
Error: Unable to locate credentials
```
**Solution:** Configure AWS credentials:
```bash
aws configure
# Or set environment variables:
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_REGION=us-west-2
```

### Browser Backend Error
```
Error fetching URL: Connection refused
```
**Solution:** Use Serper backend instead:
```bash
export SERPER_API_KEY=your_key
python deploy_agent.py --browser_backend serper ...
```

### Tool Call Parsing Error
```
Error executing ehr.load_ehr: Invalid arguments
```
**Solution:** Check that query includes proper subject_id and timestamp:
- subject_id: Must be a string (e.g., "10000032")
- timestamp: Must be 'YYYY-MM-DD HH:MM:SS' format

## Performance Tips

1. **Reduce max_rounds** for faster testing: `--max_rounds 30`
2. **Use Serper** for more reliable web search (no local backend needed)
3. **Enable verbose mode** for debugging: `--verbose`
4. **Check EHR server logs** if queries hang: `tail -f ehr_server.log`

## System Prompt

The agent uses a specialized system prompt that guides tool selection:

- **For general questions**: Use `browser.search` → `browser.open`
- **For EHR tasks**: Start with `ehr.load_ehr` → explore with `ehr.get_table_names`
- **For medical literature**: Use `ehr.retrieve_pubmed` (more focused than web search)
- **For patient analysis**: Use `ehr.run_sql_query` for complex queries

The prompt is defined in `data_utils.py` as `DEVELOPER_CONTENT_CLAUDE`.

## File Structure

```
openresearcher_ehr/
├── README.md                    # This file
├── deploy_agent.py              # Main orchestrator
├── data_utils.py                # Tool schemas and prompts
├── ehr_pool.py                  # EHR tool pool (MCP client)
├── browser.py                   # Browser tool implementation
├── test_integration.sh          # Test script
├── test_queries_ehr.jsonl       # EHR test queries
├── test_queries_hybrid.jsonl    # Hybrid test queries
├── test_queries_web.jsonl       # Web test queries
└── test_results/                # Output directory
```

## Integration with Main EHR System

This system uses the same EHR MCP server as the main AgentEHR benchmark:

- **MCP Server**: `/fsx-shared/juncheng/EHR/src/run_mcp_server.py`
- **MCP Tools**: `/fsx-shared/juncheng/EHR/src/agentlite/mcp_tools/`
- **Data**: `/fsx-shared/juncheng/EHR/data/EHRAgentBench/`

No modifications are needed to the original EHR system.

## Future Enhancements

1. **Auto-discovery**: Dynamically fetch available tools from MCP server
2. **Caching**: Cache EHR responses to reduce latency
3. **Multi-patient**: Support queries across multiple patients
4. **Streaming**: Stream tool results for faster UX
5. **vLLM Support**: Add support for local vLLM models

## Citation

If you use this integrated system, please cite both:

```bibtex
@article{agenteehr2024,
  title={AgentEHR: A Benchmark for Evaluating Autonomous Clinical Decision-Making Agents on Electronic Health Records},
  author={...},
  year={2024}
}

@article{openresearcher2024,
  title={OpenResearcher: Deep Web Research with Autonomous Agents},
  author={...},
  year={2024}
}
```
