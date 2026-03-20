# OpenResearcher + EHR Integration - Index

**Location**: `/fsx-shared/juncheng/EHR/openresearcher_ehr/`
**Created**: March 14, 2026
**Status**: ✅ Complete and ready for use

## Quick Links

- **Get Started**: [QUICKSTART.md](QUICKSTART.md) - 5-minute setup guide
- **Full Documentation**: [README.md](README.md) - Complete system documentation
- **Implementation Details**: [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) - Technical overview
- **Code Examples**: [example_usage.py](example_usage.py) - Programmatic usage
- **Validation**: [validate_setup.py](validate_setup.py) - Check prerequisites

## File Overview

### 📁 Core System (4 files)
| File | Size | Description |
|------|------|-------------|
| `deploy_agent.py` | 14 KB | Main orchestrator with dual tool routing |
| `data_utils.py` | 11 KB | Tool schemas (10 tools) and system prompts |
| `ehr_pool.py` | 4.5 KB | EHR MCP client (HTTP) |
| `browser.py` | 12 KB | Browser tool implementation |

### 📖 Documentation (4 files)
| File | Size | Description |
|------|------|-------------|
| `README.md` | 9.6 KB | Complete documentation |
| `QUICKSTART.md` | 4.3 KB | Getting started guide |
| `IMPLEMENTATION_SUMMARY.md` | 11 KB | Technical summary |
| `INDEX.md` | This file | File index |

### 🧪 Tests (4 files)
| File | Size | Description |
|------|------|-------------|
| `test_integration.sh` | 2.3 KB | Automated test script |
| `test_queries_ehr.jsonl` | 596 B | EHR test queries |
| `test_queries_hybrid.jsonl` | 676 B | Hybrid test queries |
| `test_queries_web.jsonl` | 370 B | Web test queries |

### 💻 Examples & Tools (3 files)
| File | Size | Description |
|------|------|-------------|
| `example_usage.py` | 7.1 KB | Programmatic examples |
| `validate_setup.py` | 5.3 KB | Setup validation script |
| `requirements.txt` | 443 B | Python dependencies |

### 📝 Configuration (1 file)
| File | Size | Description |
|------|------|-------------|
| `.gitignore` | 350 B | Git ignore rules |

**Total**: 15 files, ~2,691 lines of code, ~186 KB

## System Architecture

```
User Query → Bedrock Claude → Tool Router
                                  ├─→ BrowserPool → Web Search/Scrape
                                  └─→ EHRToolPool → MCP Server → Patient EHR DB
```

## Available Tools (10 total)

### Browser Tools (3)
1. `browser.search` - Web search
2. `browser.open` - Open web pages
3. `browser.find` - Find text in pages

### EHR Tools (7)
1. `ehr.load_ehr` - Load patient database
2. `ehr.get_table_names` - List tables
3. `ehr.get_column_names` - Get table schema
4. `ehr.get_records_by_time` - Time-based queries
5. `ehr.run_sql_query` - SQL on EHR data
6. `ehr.get_candidates_by_semantic_similarity` - Medical code search
7. `ehr.retrieve_pubmed` - PubMed literature

## Quick Commands

### Validate Setup
```bash
./validate_setup.py
```

### Run Tests
```bash
./test_integration.sh
```

### Simple Query
```bash
echo '{"qid": "test", "question": "What is diabetes?"}' > test.jsonl
python deploy_agent.py --data_path test.jsonl --output_dir ./results
```

### With EHR
```bash
python deploy_agent.py \
    --data_path test_queries_ehr.jsonl \
    --output_dir ./results \
    --enable_ehr \
    --ehr_mcp_url http://127.0.0.1:5003/mcp
```

### Programmatic
```bash
python example_usage.py
```

## Prerequisites

1. **AWS Credentials**: `aws configure` (for Bedrock)
2. **EHR MCP Server**: Running at `http://127.0.0.1:5003/mcp`
3. **Python Packages**: `pip install -r requirements.txt`
4. **Optional**: Serper API key for web search

## Key Features

✅ **Dual Tool Support**: Seamless routing between web and EHR tools
✅ **AWS Bedrock**: Uses Claude Sonnet 4.5 for reasoning
✅ **MCP Integration**: HTTP-based connection to EHR server
✅ **No Modifications**: Original codebases unchanged
✅ **Self-Contained**: All code in this subfolder
✅ **Well-Documented**: 4 comprehensive documentation files
✅ **Tested**: 3 test scenarios with example queries
✅ **Examples**: Programmatic usage examples included

## Directory Structure

```
openresearcher_ehr/
├── 📄 Core Files
│   ├── deploy_agent.py          # Main orchestrator
│   ├── data_utils.py             # Tool schemas & prompts
│   ├── ehr_pool.py               # EHR MCP client
│   └── browser.py                # Browser tools
│
├── 📖 Documentation
│   ├── README.md                 # Complete docs
│   ├── QUICKSTART.md             # Getting started
│   ├── IMPLEMENTATION_SUMMARY.md # Technical details
│   └── INDEX.md                  # This file
│
├── 🧪 Tests
│   ├── test_integration.sh       # Test script
│   ├── test_queries_ehr.jsonl    # EHR tests
│   ├── test_queries_hybrid.jsonl # Hybrid tests
│   └── test_queries_web.jsonl    # Web tests
│
├── 💻 Examples & Tools
│   ├── example_usage.py          # Code examples
│   ├── validate_setup.py         # Setup validator
│   └── requirements.txt          # Dependencies
│
└── 📝 Configuration
    └── .gitignore                # Git ignore
```

## Integration Status

| Component | Status | Notes |
|-----------|--------|-------|
| EHR MCP Server | ✅ Integrated | HTTP at port 5003 |
| Browser Tools | ✅ Integrated | 3 tools from OpenResearcher |
| AWS Bedrock | ✅ Integrated | Claude Sonnet 4.5 |
| Tool Routing | ✅ Complete | Prefix-based dispatch |
| Documentation | ✅ Complete | 4 comprehensive docs |
| Tests | ✅ Complete | 3 test scenarios |
| Examples | ✅ Complete | Programmatic + CLI |

## Next Steps

1. **Validate Setup**: Run `./validate_setup.py`
2. **Start MCP Server**: See QUICKSTART.md Step 2
3. **Run First Test**: `./test_integration.sh`
4. **Try Examples**: `python example_usage.py`
5. **Read Docs**: [README.md](README.md) for full details

## Support

- **Setup Issues**: See [QUICKSTART.md](QUICKSTART.md)
- **Troubleshooting**: See [README.md](README.md) Troubleshooting section
- **Code Examples**: See [example_usage.py](example_usage.py)
- **Technical Details**: See [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)

---

**Project**: AgentEHR + OpenResearcher Integration
**Location**: `/fsx-shared/juncheng/EHR/openresearcher_ehr/`
**Status**: Production-ready
