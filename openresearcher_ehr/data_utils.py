"""
Data utilities for OpenResearcher with EHR integration.
Combines browser tools with EHR clinical reasoning tools.
"""
import json

# System prompts for different model types
DEVELOPER_CONTENT = """
You are a helpful assistant and harmless assistant.

You will be able to use a set of browsering tools to answer user queries.

Tool for browsing.
The `cursor` appears in brackets before each browsing display: `[{cursor}]`.
Cite information from the tool using the following format:
`【{cursor}†L{line_start}(-L{line_end})?】`, for example: `【6†L9-L11】` or `【8†L3】`.
Do not quote more than 10 words directly from the tool output.
sources=web
""".strip()

DEVELOPER_CONTENT_CLAUDE = """
You are a research assistant with access to both web browsing and clinical EHR tools.

**Browser Tools** (for general web research):
- browser.search: Search the web for information
- browser.open: Open and read web pages
- browser.find: Find text within pages

**EHR Tools** (for clinical data and medical research):
- ehr.load_ehr: Load patient EHR database (must be called first)
- ehr.get_table_names: List available patient data tables
- ehr.get_column_names: Get table column information
- ehr.get_records_by_time: Query patient records within time range
- ehr.run_sql_query: Execute SQL queries on patient database
- ehr.get_candidates_by_semantic_similarity: Search medical terminology
- ehr.retrieve_pubmed: Search PubMed medical literature

**Workflow:**
1. For clinical EHR tasks: Start with ehr.load_ehr to initialize patient context
2. For general questions: Use browser.search
3. For clinical queries: Use ehr.get_table_names to explore available data
4. For patient analysis: Use ehr.get_records_by_time and ehr.run_sql_query
5. For medical terminology: Use ehr.get_candidates_by_semantic_similarity
6. For medical literature: Use ehr.retrieve_pubmed (more focused than web search)

The `cursor` appears in brackets before each browsing display: `[{cursor}]`.
Cite web sources using: 【{cursor}†L{line_start}(-L{line_end})?】

Your final response should be in the following format:
Explanation: {{your explanation for your final answer with inline citations}}
Exact Answer: {{your succinct, final answer}}
Confidence: {{your confidence score between 0% and 100% for your answer}}

sources=web,ehr
"""

# Browser tool definitions (from OpenResearcher)
BROWSER_TOOL_CONTENT = """
[
  {
    "type": "function",
    "function": {
      "name": "browser.search",
      "description": "Searches for information related to a query and displays top N results. Returns a list of search results with titles, URLs, and summaries.",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "The search query string"
          },
          "topn": {
            "type": "integer",
            "description": "Number of results to display",
            "default": 10
          }
        },
        "required": ["query"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "browser.open",
      "description": "Opens a link from the current page or a fully qualified URL. Can scroll to a specific location and display a specific number of lines. Valid link ids are displayed with the formatting: 【{id}†.*】.",
      "parameters": {
        "type": "object",
        "properties": {
          "id": {
            "type": ["integer", "string"],
            "description": "Link id from current page (integer) or fully qualified URL (string). Default is -1 (most recent page)",
            "default": -1
          },
          "cursor": {
            "type": "integer",
            "description": "Page cursor to operate on. If not provided, the most recent page is implied",
            "default": -1
          },
          "loc": {
            "type": "integer",
            "description": "Starting line number. If not provided, viewport will be positioned at the beginning or centered on relevant passage",
            "default": -1
          },
          "num_lines": {
            "type": "integer",
            "description": "Number of lines to display",
            "default": -1
          },
          "view_source": {
            "type": "boolean",
            "description": "Whether to view page source",
            "default": false
          },
          "source": {
            "type": "string",
            "description": "The source identifier (e.g., 'web')"
          }
        },
        "required": []
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "browser.find",
      "description": "Finds exact matches of a pattern in the current page or a specified page by cursor.",
      "parameters": {
        "type": "object",
        "properties": {
          "pattern": {
            "type": "string",
            "description": "The exact text pattern to search for"
          },
          "cursor": {
            "type": "integer",
            "description": "Page cursor to search in. If not provided, searches in the current page",
            "default": -1
          }
        },
        "required": ["pattern"]
      }
    }
  }
]
""".strip()

# EHR tool definitions (core 7 tools selected for clinical reasoning)
EHR_TOOL_CONTENT = """
[
  {
    "type": "function",
    "function": {
      "name": "ehr.load_ehr",
      "description": "Load the EHR database for a specific patient. This action MUST be taken once at the beginning of each EHR-related task.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient (e.g., '10000032')"
          },
          "timestamp": {
            "type": "string",
            "description": "The current timestamp in 'YYYY-MM-DD HH:MM:SS' format (e.g., '2150-12-01 10:00:00')"
          }
        },
        "required": ["subject_id", "timestamp"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_table_names",
      "description": "Retrieves the names of all available tables in the patient's EHR database, categorized into EHR tables and candidate tables. Use this to explore what data is available.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient (e.g., '10000032')"
          }
        },
        "required": ["subject_id"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_column_names",
      "description": "Retrieves all column names for a specified table. Essential for understanding table structure before querying.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient"
          },
          "table_name": {
            "type": "string",
            "description": "The name of the table (e.g., 'admissions', 'd_icd_diagnoses')"
          }
        },
        "required": ["subject_id", "table_name"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_records_by_time",
      "description": "Finds records in an EHR table that fall within a given time range. Useful for getting detailed event data for a specific period.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient"
          },
          "table_name": {
            "type": "string",
            "description": "The name of the table to search in (e.g., 'admissions', 'labevents')"
          },
          "start_time": {
            "type": "string",
            "description": "The start of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format"
          },
          "end_time": {
            "type": "string",
            "description": "The end of the time range in 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' format"
          }
        },
        "required": ["subject_id", "table_name", "start_time", "end_time"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.run_sql_query",
      "description": "Executes a standard SQL query against the patient's EHR tables. Use this for complex filtering, joins, aggregations, or finding trends across multiple tables.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient"
          },
          "sql_query": {
            "type": "string",
            "description": "A valid SQL query string (e.g., SELECT * FROM labevents WHERE valuenum > 5)"
          }
        },
        "required": ["subject_id", "sql_query"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_candidates_by_semantic_similarity",
      "description": "Searches for medical terminology candidates using semantic similarity. Useful for finding ICD codes, lab test names, or medications when you have a description.",
      "parameters": {
        "type": "object",
        "properties": {
          "table_name": {
            "type": "string",
            "description": "The candidate table name (e.g., 'd_icd_diagnoses', 'd_labitems')"
          },
          "query": {
            "type": "string",
            "description": "The medical term or description to search for"
          },
          "top_k": {
            "type": "integer",
            "description": "Number of top results to return",
            "default": 10
          }
        },
        "required": ["table_name", "query"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.retrieve_pubmed",
      "description": "Searches PubMed for medical literature related to a query. More focused than general web search for medical topics.",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "The medical query to search for in PubMed"
          },
          "top_k": {
            "type": "integer",
            "description": "Number of top results to return",
            "default": 5
          }
        },
        "required": ["query"]
      }
    }
  }
]
""".strip()

# Combined tool content (browser + EHR)
def get_combined_tools():
    """Get combined browser and EHR tools as a list."""
    browser_tools = json.loads(BROWSER_TOOL_CONTENT)
    ehr_tools = json.loads(EHR_TOOL_CONTENT)
    return browser_tools + ehr_tools

# Export as JSON string for compatibility
COMBINED_TOOL_CONTENT = json.dumps(get_combined_tools())

# For backward compatibility - just browser tools
TOOL_CONTENT = BROWSER_TOOL_CONTENT

# EHR Tool Content - ALL 20 tools from AgentEHR MCP server
EHR_TOOL_CONTENT_JSON = '''
[
  {
    "type": "function",
    "function": {
      "name": "ehr.load_ehr",
      "description": "Load the EHR database for a specific patient. This action MUST be taken once at the beginning of each EHR-related task.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "The unique identifier for the patient (e.g., '10000032')"
          },
          "timestamp": {
            "type": "string",
            "description": "The current timestamp in 'YYYY-MM-DD HH:MM:SS' format"
          }
        },
        "required": [
          "subject_id",
          "timestamp"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_table_names",
      "description": "Retrieves the names of all available tables in the database, categorized into EHR tables and candidate tables.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "Patient ID"
          }
        },
        "required": [
          "subject_id"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_column_names",
      "description": "Retrieves all column names for a specified table.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string",
            "description": "Patient ID"
          },
          "table_name": {
            "type": "string",
            "description": "Table name (e.g., 'admissions')"
          }
        },
        "required": [
          "subject_id",
          "table_name"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_table_description",
      "description": "Retrieve table description and column information from database schema.",
      "parameters": {
        "type": "object",
        "properties": {
          "table_name": {
            "type": "string",
            "description": "Table name"
          }
        },
        "required": [
          "table_name"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_unique_values",
      "description": "Retrieves all unique values from a categorical column in an EHR table.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "table_name": {
            "type": "string"
          },
          "column_name": {
            "type": "string"
          }
        },
        "required": [
          "subject_id",
          "table_name",
          "column_name"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_records_by_time",
      "description": "Finds records in an EHR table within a time range.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "table_name": {
            "type": "string"
          },
          "start_time": {
            "type": "string",
            "description": "YYYY-MM-DD HH:MM:SS"
          },
          "end_time": {
            "type": "string",
            "description": "YYYY-MM-DD HH:MM:SS"
          }
        },
        "required": [
          "subject_id",
          "table_name",
          "start_time",
          "end_time"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_event_counts_by_time",
      "description": "Calculates the number of events in all EHR tables within a time range.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "start_time": {
            "type": "string"
          },
          "end_time": {
            "type": "string"
          }
        },
        "required": [
          "subject_id",
          "start_time",
          "end_time"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_latest_records",
      "description": "Finds the latest timestamp and returns all records with that timestamp.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "table_name": {
            "type": "string"
          }
        },
        "required": [
          "subject_id",
          "table_name"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_records_by_keyword",
      "description": "Searches text columns of an EHR table for a keyword.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "table_name": {
            "type": "string"
          },
          "keyword": {
            "type": "string"
          }
        },
        "required": [
          "subject_id",
          "table_name",
          "keyword"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_records_by_value",
      "description": "Finds records where a column exactly matches a value.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "table_name": {
            "type": "string"
          },
          "column_name": {
            "type": "string"
          },
          "value": {
            "type": "string"
          }
        },
        "required": [
          "subject_id",
          "table_name",
          "column_name",
          "value"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.run_sql_query",
      "description": "Executes a SQL query against the patient's EHR tables.",
      "parameters": {
        "type": "object",
        "properties": {
          "subject_id": {
            "type": "string"
          },
          "sql_query": {
            "type": "string",
            "description": "Valid SQL query"
          }
        },
        "required": [
          "subject_id",
          "sql_query"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_candidates_by_keyword",
      "description": "Searches candidate table text columns for a keyword.",
      "parameters": {
        "type": "object",
        "properties": {
          "table_name": {
            "type": "string"
          },
          "keyword": {
            "type": "string"
          }
        },
        "required": [
          "table_name",
          "keyword"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_candidates_by_fuzzy_matching",
      "description": "Finds similar items in candidate table using fuzzy matching.",
      "parameters": {
        "type": "object",
        "properties": {
          "table_name": {
            "type": "string"
          },
          "query": {
            "type": "string"
          },
          "top_k": {
            "type": "integer",
            "default": 10
          }
        },
        "required": [
          "table_name",
          "query"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.get_candidates_by_semantic_similarity",
      "description": "Semantic search for similar medical terms using embeddings.",
      "parameters": {
        "type": "object",
        "properties": {
          "table_name": {
            "type": "string"
          },
          "query": {
            "type": "string"
          },
          "top_k": {
            "type": "integer",
            "default": 10
          }
        },
        "required": [
          "table_name",
          "query"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.think",
      "description": "Synthesize information and articulate next actions.",
      "parameters": {
        "type": "object",
        "properties": {
          "response": {
            "type": "string",
            "description": "Thought process content"
          }
        },
        "required": [
          "response"
        ]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ehr.finish",
      "description": "Final step - provide clinical predictions.",
      "parameters": {
        "type": "object",
        "properties": {
          "response": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "List of clinical predictions"
          }
        },
        "required": [
          "response"
        ]
      }
    }
  }
]

'''

# Function to get combined tools
def get_combined_tools_with_all_ehr():
    """Get browser tools + 16 EHR tools (knowledge retrieval tools excluded, using web search instead)."""
    browser_tools = json.loads(BROWSER_TOOL_CONTENT)
    ehr_tools = json.loads(EHR_TOOL_CONTENT_JSON)
    return browser_tools + ehr_tools

# For use in deploy_agent.py - update COMBINED_TOOL_CONTENT to use all tools
COMBINED_TOOL_CONTENT_FULL = json.dumps(get_combined_tools_with_all_ehr())
