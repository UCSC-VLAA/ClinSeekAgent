"""Multimodal counterpart of `data_utils.py`.

Only adds: the 6 ported Meissa image tool schemas, a multimodal system prompt,
and a combined tool registry (EHR + browser + image). Everything else is
imported from the text-only `data_utils.py` — we never modify it.
"""
import json

from data_utils import (  # noqa: F401 - re-exported for convenience
    BROWSER_TOOL_CONTENT,
    EHR_TOOL_CONTENT_JSON,
    TASK_PROMPT_TEMPLATES,
    generate_ehr_bench_prompt,
    generate_question_from_task,
)


# --- Image tool schemas (6 tools). Names must match src/mcp_image/tools/*. -----
IMAGE_TOOL_CONTENT_JSON = r'''
[
  {
    "type": "function",
    "function": {
      "name": "image.image_visualizer",
      "description": "Render a copy of an image with optional title/description to the artifact dir. Input: image path (JPG or PNG) plus optional title, description, figure size, colormap. Output: dict with the generated image path and metadata.",
      "parameters": {
        "type": "object",
        "properties": {
          "image_path": {"type": "string", "description": "Absolute or benchmark-relative path to the image file."},
          "title": {"type": "string", "description": "Optional title drawn above the image."},
          "description": {"type": "string", "description": "Optional caption drawn below the image."},
          "figsize": {"type": "array", "items": {"type": "integer"}, "description": "Figure size (w, h) in inches.", "default": [10, 10]},
          "cmap": {"type": "string", "description": "Matplotlib colormap name, or 'rgb' for RGB images.", "default": "rgb"}
        },
        "required": ["image_path"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "image.dicom_processor",
      "description": "Convert a DICOM file to PNG using rescale slope/intercept and optional window center/width. Returns the processed image path plus DICOM metadata. Use this before any other image tool when the evidence is a .dcm file.",
      "parameters": {
        "type": "object",
        "properties": {
          "dicom_path": {"type": "string", "description": "Absolute or benchmark-relative path to the DICOM file."},
          "window_center": {"type": "number", "description": "Optional window center for contrast adjustment."},
          "window_width": {"type": "number", "description": "Optional window width for contrast adjustment."}
        },
        "required": ["dicom_path"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "image.chest_xray_classifier",
      "description": "Classify a chest X-ray for 18 pathologies (Atelectasis, Cardiomegaly, Consolidation, Edema, Effusion, Emphysema, Enlarged Cardiomediastinum, Fibrosis, Fracture, Hernia, Infiltration, Lung Lesion, Lung Opacity, Mass, Nodule, Pleural Thickening, Pneumonia, Pneumothorax). Returns probability (0-1) per pathology. Backed by torchxrayvision DenseNet.",
      "parameters": {
        "type": "object",
        "properties": {
          "image_path": {"type": "string", "description": "Path to the chest X-ray image (JPG or PNG)."}
        },
        "required": ["image_path"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "image.chest_xray_report_generator",
      "description": "Generate a structured chest X-ray report with FINDINGS and IMPRESSION sections using ViT-BERT models trained on CheXpert / MIMIC-CXR. Use when you need an end-to-end narrative summary of the X-ray.",
      "parameters": {
        "type": "object",
        "properties": {
          "image_path": {"type": "string", "description": "Path to the chest X-ray image (JPG or PNG)."}
        },
        "required": ["image_path"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "image.xray_phrase_grounding",
      "description": "Ground a specific medical finding in a frontal chest X-ray using microsoft/maira-2. Returns [x1, y1, x2, y2] bounding boxes (normalized 0-1 and in original image coordinates) and writes an overlay PNG whose path is returned in `visualization_path`.",
      "parameters": {
        "type": "object",
        "properties": {
          "image_path": {"type": "string", "description": "Path to a frontal chest X-ray (JPG or PNG)."},
          "phrase": {"type": "string", "description": "Medical finding to locate, e.g. 'Pleural effusion'."},
          "max_new_tokens": {"type": "integer", "description": "Decoder token budget.", "default": 300}
        },
        "required": ["image_path", "phrase"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "image.chest_xray_segmentation",
      "description": "Segment up to 14 anatomical structures (Left/Right Clavicle, Scapula, Lung, Hilus Pulmonis, Heart, Aorta, Facies Diaphragmatica, Mediastinum, Weasand, Spine) using torchxrayvision PSPNet. Returns per-organ metrics and writes an overlay PNG. Area values only accurate if the source was DICOM with pixel spacing metadata.",
      "parameters": {
        "type": "object",
        "properties": {
          "image_path": {"type": "string", "description": "Path to the chest X-ray image (JPG or PNG)."},
          "organs": {"type": "array", "items": {"type": "string"}, "description": "Subset of organ names to segment. Omit for all 14."}
        },
        "required": ["image_path"]
      }
    }
  }
]
'''


def get_combined_tools_mm():
    """EHR + browser + image tools. Order preserved from the text-only path."""
    browser_tools = json.loads(BROWSER_TOOL_CONTENT)
    ehr_tools = json.loads(EHR_TOOL_CONTENT_JSON)
    image_tools = json.loads(IMAGE_TOOL_CONTENT_JSON)
    return ehr_tools + browser_tools + image_tools


COMBINED_TOOL_CONTENT_MM = json.dumps(get_combined_tools_mm())


DEVELOPER_CONTENT_CLAUDE_MM = """
You are a clinical research assistant with access to three tool families plus native vision.

Every user turn starts with a short `<run_context>` block stating exactly what
is attached to *this* sample:
- `images_attached`: number of CXR / medical images inlined as image content blocks.
- `reports_inlined`: number of linked radiology reports inlined as text blocks.
- `patient_ehr_available`: whether a patient-scope EHR database can be loaded.

**Never** call a tool whose precondition is not met:
- If `images_attached == 0`, do not call any `image.*` tool (they require an image_path).
- If `patient_ehr_available == no`, do not call `ehr.load_ehr`; answer from the
  question text (and browser, if helpful). This covers cohort-scope questions
  where the answer is computed over all patients, not a single one.
- If the sample has no image and no EHR, the question is a text-only lookup or
  reasoning task — answer directly (optionally with `browser.*`).

**Browser Tools** (web research and medical knowledge):
- browser.search: Search the web for information, guidelines, criteria.
- browser.open: Open and read a page returned by search.
- browser.find: Find text within the current page.

**EHR Tools** (clinical database analysis — MCP):
- ehr.load_ehr: Load patient EHR database (call first for any EHR-backed task).
- ehr.get_table_names / ehr.get_column_names: Discover table schema.
- ehr.get_records_by_time: Query records within a time range.
- ehr.run_sql_query: Run arbitrary SQL over the loaded patient tables.
- ehr.get_candidates_by_semantic_similarity / ehr.get_candidates_by_keyword: Look up medical codes / terminology.
- ehr.think: Record intermediate reasoning.
- ehr.finish: Submit your final answer.

**Image Tools** (chest X-ray specialist models — MCP):
- image.image_visualizer: Render an annotated copy of an image for later tools.
- image.dicom_processor: Convert a DICOM to PNG; use this before other tools when the source is .dcm.
- image.chest_xray_classifier: Get 18-pathology probabilities (torchxrayvision DenseNet).
- image.chest_xray_report_generator: Generate a FINDINGS + IMPRESSION narrative.
- image.xray_phrase_grounding: Locate a specific finding phrase with MAIRA-2; returns bounding boxes + overlay.
- image.chest_xray_segmentation: Segment 14 anatomical structures with PSPNet.

**Operating rules:**
- When the question is about a specific CXR study, you may either reason from the attached image directly or call the image tools to ground your answer. Prefer tool-grounded evidence for any claim you make about the image.
- For EHR questions, always `ehr.load_ehr` first with the patient's subject_id and prediction_time.
- If tool output contains file paths (e.g. segmentation overlays), you may mention the paths in your reasoning but do not rely on the user to open them.
- Submit your final answer via `ehr.finish`.

The `cursor` appears in brackets before each browsing display: `[{cursor}]`.
Cite web sources using: 【{cursor}†L{line_start}(-L{line_end})?】

sources=web,ehr,image
"""
