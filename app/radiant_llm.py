# radiant_llm: Empowering Radiation Protection with AI-Driven Solutions
"""
chatbots/radiant_llm.py

Primary runtime: FastAPI (api.py / radiant-llm-api) + React frontend.
Exports global `cb` (Chatbot) and `convchain_api` for REST/streaming.

The legacy Dash UI (layout/register_callbacks) is deprecated and raises if invoked.
"""
# ── heavy imports (LangChain, Nougat, etc.) ───────────────────────────
import os
import copy
import matplotlib.pyplot as plt
from difflib import get_close_matches
import base64
import openai
import google.generativeai as genai
import pandas as pd
import warnings
import logging
import fitz  # PyMuPDF
from langchain.schema import Document
from langchain_openai import OpenAIEmbeddings
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from langchain_chroma import Chroma
import tempfile                          # To create a temposral directory for chromadb when running as an executable
from langchain.chains import RetrievalQAWithSourcesChain
import socket

# Additional imports for LangChain agent setup
import webbrowser
import subprocess
import datetime as _dt
import shlex    # For SAM execution 
from tqdm import tqdm  # For SAM execution on cluster
from langchain_experimental.utilities import PythonREPL
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
# from langchain_core.tools import BaseTool, tool
from langchain.agents import create_tool_calling_agent, create_openai_tools_agent, AgentExecutor
from langchain_openai import ChatOpenAI

# Memory capabilities 
from langchain.prompts import MessagesPlaceholder
from langchain.memory import ConversationBufferWindowMemory
from langchain.agents import initialize_agent
from langchain_community.agent_toolkits.load_tools import load_tools

from typing import  Annotated, Sequence, Callable
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# Additional imports for other functionalities
import requests
from bs4 import BeautifulSoup
import logging
import wikipedia
import json
import yaml
from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
from langchain_google_genai.embeddings import GoogleGenerativeAIEmbeddings

# nougat pdf predictor
import io
from transformers import AutoProcessor, VisionEncoderDecoderModel
from transformers import StoppingCriteria, StoppingCriteriaList
from collections import defaultdict
import torch
from pathlib import Path
from collections import defaultdict
from typing import Optional, Set, List, Dict, Any, Tuple
from io import BytesIO
import json
import mimetypes              # For image Analysis tool
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Traditional PDF Processor?
from langchain_community.document_loaders import PyPDFLoader
import fitz
import re
import time
import pytesseract

# Dash Components 
import dash
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc
from dash import html # Assuming you are using dash.html

# *************** More for ERROR RESOLUTION PIPELINE ***************
import yaml
import difflib
from sklearn.feature_extraction.text import TfidfVectorizer
# from sklearn.metrics.pairwise import cosine_similarity
from langchain.callbacks.manager import CallbackManager

# Custom Tools
#
from tools.pdf_tools import VisualParserPDFAnalyser
from tools.data_tools import CSVDataFinder

from tools.data_tools import csv_excel_reader
from tools.data_tools import file_downloader
from tools.data_tools import text_file_reader
from tools.web_tools import web_scraper
from tools.web_tools import web_search
from tools.web_tools import wikipedia_search
from tools.general_tools import URLValidation

# from tools.pdf_tools import FullPDFAnalysis_query
# from PDFAnalyser import PDFAnalyser as LegacyPDFAnalyser
from tools.image_tools import image_analysis
from tools.general_tools import PythonREPLTool

# Custom Utilities
from utils.general_utilities import MarkdownParser
from utils.general_utilities import LLMMarkdownParser
from utils.general_utilities import sanitize_user_markdown
from utils.general_utilities import free_port_finder
from utils.general_utilities import LLMReasoningChainLogger
from utils.general_utilities import ReasoningLoggerCallback
from utils.general_utilities import yaml_reader
from utils.pdf_helpers import knowledge_base_sanitizer
from utils.alerts import global_external_alerts
from utils.radiant_skill_loader import (
    DEFAULT_MAX_USER_SKILL_FILE_BYTES,
    DEFAULT_SKILL_CONTEXT_CHAR_BUDGET,
    RADIANT_AVAILABLE_SKILL_MODULES,
    SKILL_LOADING_PRESETS,
    SkillLoadResult,
    build_radiant_skill_catalog_block,
    build_radiant_skill_context_with_meta,
    discover_radiant_skill_catalog,
    radiant_skill_catalog_entries,
    resolve_bundled_skills_root,
    resolve_skill_loading_preset,
    scan_user_skills_directory,
)
from tools.skill_lookup_tool import skill_lookup
from tools.session_tools import make_search_past_sessions_tool
from utils.grace_vllm import (
    GRACE_MODEL_ID,
    GRACE_VLLM_NATIVE_TOOLS_HINT,
    build_grace_chat_openai,
    check_vllm_native_tool_support,
    check_vllm_reachable,
    get_vllm_max_model_len,
    grace_skill_context_char_budget,
    grace_system_prompt_for_context,
    grace_tools,
    load_vllm_config,
)
from utils.local_embeddings import (
    DEFAULT_LOCAL_EMBEDDING_DIM,
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    build_local_embeddings,
)
from utils.radiant_runtime_info import format_runtime_context, apply_iteration_marker

#
warnings.filterwarnings("ignore", category=UserWarning, module='pydantic')
# -------------------------------

# Load environment variables from the .env file
load_dotenv()
# Access API keys
# ======================================================================================
openai_key   = os.getenv('OPENAI_API_KEY')
gemini_key   = os.getenv('GEMINI_API_KEY')
openai_free  = os.getenv('OPENAI_API_FREE')
langchain_key = os.getenv('LANGCHAIN_API_KEY')
cse_key       = os.getenv('CUSTOM_SEARCH_ENGINE_API_KEY')   # Google Custom Search engine
cse_id        = os.getenv('CUSTOM_SEARCH_ENGINE_ID')        # Custom search ID

# Trace on LangChain
# Set environment variables
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"]    = langchain_key
os.environ["OPENAI_API_KEY"]       = openai_key

#############################################################################################
######################################### RADIANT-LLM TOOLS #################################
#############################################################################################

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# PDF Processor Tool Based on NOUGAT
# #++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def PDFReaderTool(
                query:str, 
                working_directory:str, 
                # index_file:str = 'documents_library.json', 
                pdf_name: str = None,
                rebuild_vector_store: bool = False,
                text_mode: str = "nougat",
                vision_detail: str = "low",
                metadata_pages: int = 2,
                max_workers: int = 1,
                gpt_reasoning_effort: str = "medium",
                log_level: str = "INFO",
                ) -> str:
    """
    Processes new PDFs, updates JSON library, creates embeddings, updates vector store, and performs a query.
       
    Args:
        query (str): The query to be answered by the system.
        working_directory (str): Path to the directory containing PDFs.
        pdf_name (str, optional): If provided, restrict retrieval to this single PDF (basename, e.g. 'paper.pdf').
        rebuild_vector_store (bool): Force vector store rebuild even without new PDFs.
        text_mode (str): "nougat" or "lightweight".
        vision_detail (str): "low", "high", or "auto".
        metadata_pages (int): Number of front pages for metadata extraction.
        max_workers (int): Thread workers used during parsing.
        gpt_reasoning_effort (str): GPT reasoning effort: none|low|medium|high|xhigh.
        log_level (str): Parser log level.
     
    Returns:
        str: The response to the query.
    """
    return VisualParserPDFAnalyser(
        query=query,
        working_directory=working_directory,
        cb=cb,
        global_external_alerts=global_external_alerts,
        pdf_name=pdf_name,
        rebuild_vector_store=rebuild_vector_store,
        text_mode=text_mode,
        vision_detail=vision_detail,
        metadata_pages=metadata_pages,
        max_workers=max_workers,
        gpt_reasoning_effort=gpt_reasoning_effort,
        log_level=log_level,
    )

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Tool to read and summarize a CSV or Excel file contents
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def CSVandExcelFileParserTool(working_directory: str, 
                          file_name: str = None,
                          ) -> str:
    """
    Reads a CSV or Excel file and summarizes its contents.

    Parameters:
    working_directory (str): The path to the directory containing files.
    file_name (str, optional): The name of the file (CSV or Excel) to read. If not provided, the first CSV or Excel file found in the directory will be used.

    Returns:
    dict: A summary of the file contents, or an error message.
    """
    return csv_excel_reader(working_directory, file_name, alert_sink=global_external_alerts)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Unified Text File Reader Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

@tool
def TextFileReaderTool(file_path: str) -> str:
    """
    Reads and returns the contents of a file. Supports:
    - Plain text files (.txt, .py, .m, .inp, .i, .md, .toml, .cfg, .ini, .rst, etc.)
    - JSON files (.json)
    - YAML files (.yaml, .yml)

    Parameters:
    file_path (str): The full path to the file.

    Returns:
    str: File contents or an error message.
    """
    return text_file_reader(file_path, alert_sink=global_external_alerts)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Skill Lookup Tool — on-demand skill pack loader
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

_APP_ROOT = Path(__file__).resolve().parent

@tool
def SkillLookupTool(pack_name: str) -> str:
    """Retrieve the full content and available scripts of any skill pack — user-defined or bundled.
    Call this BEFORE following any skill-specific workflow or running any skill script.
    Pass the pack name exactly or approximately as shown in the Skill Packs Catalog."""
    return skill_lookup(
        pack_name,
        cb.skills_directory or "",
        str(_APP_ROOT),
        alert_sink=global_external_alerts,
    )

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Tool to download any file from a web url
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def FileDownloaderTool(url_input: str, working_directory: str) -> str:
    """
    Downloads files (images, PDFs, etc.) from the provided URLs and saves them to the working directory.
    Only downloads files with supported extensions.

    Args:
        url_input (str): A single URL or multiple URLs (comma-separated).
        working_directory (str): The directory to save the downloaded files.

    Returns:
        str: A
    """
    return file_downloader(url_input, working_directory)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Image Analysis Tool 
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def ImageAnalysisTool(query: Optional[str], image_directory: str,) -> Dict[str, List[Dict[str, Any]]]:
    
    """     
    Scans a directory, sends any *new* images to GPT-4o for description,
    update a Chroma vector-store + JSON log, and return the descriptions
    that match an optional query.
    """
    return image_analysis(query, image_directory, chatbot = cb, alert_sink=global_external_alerts)



#############################################################################################
######################################### WEB SEARCH  TOOL ##################################
#############################################################################################

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# URL verification 
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def URLValidationTool(url: str) -> bool:
    """
    Checks if a URL is valid and reachable without downloading the entire content.

    This function sends a lightweight HEAD request, which only retrieves
    the response headers, making it much faster and more efficient
    than a full GET request for link verification.

    Args:
        url (str): The URL to check.
        timeout (int): The number of seconds to wait for a response.

    Returns:
        bool: True if the URL is valid and returns a 2xx status code, False otherwise.
    """

    return URLValidation(url, timeout= 5)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Web Scraper Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def WebScraperTool(url_input: str) -> str:
    """
    Scrapes the provided web pages for detailed information.

    Args:
        url_input (str): A single URL or multiple URLs separated by commas.

    Returns:
        str: A summary of the scraped content or error messages for inaccessible pages.
    """
    # return WebScraperTool(url_input, alert_sink=global_external_alerts)
    return web_scraper(url_input, alert_sink=global_external_alerts)


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Web Search Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def WebSearchTool(query: str) -> str:
    """
    Run a web search (Tavily, falling back to Google Custom Search) and get top results.
    Args:
        input (str): A single query or URL to be searched.

    Returns:
        str: A summary of the scraped content or error messages for inaccessible pages.
    """
    return web_search(query, alert_sink=global_external_alerts)


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Wikipedia Search
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
@tool
def WikipediaSearchTool(query: str) -> str:
    """Search Wikipedia to retreive page summaries.
    """
    return wikipedia_search(query, alert_sink=[])

#=========================================================================================
# RAG-ing from CSV and EXCEL FILES
#=========================================================================================
# @tool("csv-data-finder", return_direct=True)
@tool
def CSVDataFinderTool(working_directory: str,
                      file_name: Optional[str],
                      query: str
                    ) -> str:
                        
    """
    A comprehensive function to query data from a CSV or Excel file using a
    LangChain pandas dataframe agent. It intelligently detects the file type
    (.csv, .xls, .xlsx) based on the provided file_path. If the file_path
    does not have an extension, it attempts to auto-detect by trying common
    CSV/Excel extensions.

    Args:
        file_path (str): The path to the CSV or Excel file. Can be 'data.csv',
                         'data.xlsx', or just 'data' (for auto-detection).
        query (str): The natural language query to execute against the file's data.

    Returns:
        str: The result of the query from the CSV/Excel data, or an error message.
    """

    return CSVDataFinder(working_directory=working_directory,
                         file_name=file_name,
                         query=query,
                         cb=cb,
                         alert_sink=global_external_alerts
                         )
                         

# =========================================================================================
# Knowledge Base Sanitizer Tool
# =========================================================================================
@tool
def PDFKnowledgeBaseSanitizerTool(
                            working_directory: str,
                            pdfs_to_remove: Set[str],
                        ) -> Dict[str, Any]:
    """
    Remove entries for the specified PDF(s) from all KB `.jsonl` files in `working_directory`.
    Creates a one-time backup per KB file, then rewrites the KB files in-place.
    """
    return knowledge_base_sanitizer(
        working_directory=working_directory,
        pdfs_to_remove=pdfs_to_remove,
        backup_suffix="_backup_before_cleanup",
        recursive=False,
        only_kb_filenames={"01_chunks_kb.jsonl", "02_visuals_kb.jsonl", "03_metadata_kb.jsonl"},
    )

# ==================================== End of Tool ========================================


# =========================================================================================
# MERGE THE TOOLS 
# =========================================================================================
tools = [PDFReaderTool,
         PDFKnowledgeBaseSanitizerTool,
         URLValidationTool,
         WebSearchTool,
         WebScraperTool,
         WikipediaSearchTool,
         PythonREPLTool,
         ImageAnalysisTool,
         CSVandExcelFileParserTool,
         CSVDataFinderTool,
         TextFileReaderTool,
         SkillLookupTool,
         FileDownloaderTool]
# =====================================================================================
# Create the Chatbot
# =====================================================================================

# ---------------------------------------------------------------------#
# A.  Global helper objects (Chatbot instance, JSON prompt, etc.)      #
# ---------------------------------------------------------------------#

# # -------------------------------
# # Load JSON files for prompt template and basic queries
# # -------------------------------
# current_dir = os.path.dirname(os.path.abspath(__file__))
# # system_prompt_sam_with_few_shots_a
# prompt_file_path = os.path.join(current_dir, "system_prompt_radiant_llm.json")
# basic_queries_path = os.path.join(current_dir, "basic_queries_radiant_llm.json")

# with open(prompt_file_path, "r", encoding="utf-8") as file:
#     prompt_content = file.read()

# with open(basic_queries_path, "r") as file:
#     basic_queries = json.load(file)["basic_queries"]

# -------------------------------
# Load YAML files for prompt template and basic queries using the custom reader
# -------------------------------
# NOTE:
# When this project is installed as a package (e.g., inside Docker),
# `__file__` may live under site-packages while the YAML config files are
# shipped/copied elsewhere (e.g., /radiant-llm). So we resolve config paths
# robustly across common locations.
def resolveConfigPath(filename: str) -> Path:
    candidates = []

    # 1) Same directory as this module
    candidates.append(Path(__file__).resolve().parent / filename)

    # 2) Current working directory
    candidates.append(Path.cwd() / filename)

    # 3) Docker default app directory
    candidates.append(Path("/radiant-llm") / filename)

    # 4) Optional override via env var
    cfg_dir = os.getenv("RADIANT_LLM_CONFIG_DIR")
    if cfg_dir:
        candidates.append(Path(cfg_dir) / filename)

    for p in candidates:
        if p.exists():
            return p

    # Return the first candidate for a helpful error message downstream
    return candidates[0]


prompt_file_path = resolveConfigPath("system_prompt_radiant_llm.yml")
basic_queries_path = resolveConfigPath("basic_queries_radiant_llm.yml")

# prompt_file_path = os.path.join(current_dir, r"prompts\system_prompt_radiant_llm.yml")
# basic_queries_path = os.path.join(current_dir, r"prompts\basic_queries_radiant_llm.yml")

# Load the prompt content and flatten it into a single string (default behavior)
prompt_content, prompt_error = yaml_reader(prompt_file_path, flatten=True)

if prompt_error:
    print(f"Error loading system prompt YAML: {prompt_error}")
    raise SystemExit(prompt_error)

# Load the basic queries as a Python dictionary (disable flattening)
basic_queries_data, queries_error = yaml_reader(basic_queries_path, flatten=False)

if queries_error:
    print(f"Error loading basic queries YAML: {queries_error}")
    raise SystemExit(queries_error)
    
# Extract the list of queries from the loaded dictionary
basic_queries = basic_queries_data.get("basic_queries", [])

# Create a temporary directory (as used in the Panel version)
temp_dir = tempfile.mkdtemp()

# Model-specific reasoning effort options (per OpenAI docs). Empty = no reasoning_effort support.
REASONING_EFFORT_MAP = {
    "gpt-5": ["minimal", "low", "medium", "high"],
    "gpt-5.1": ["none", "low", "medium", "high"],
    "gpt-5.2": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.3-chat-latest": [],  # no documented reasoning_effort
    "gpt-5.4": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.5": ["none", "low", "medium", "high", "xhigh"],
    "gpt-4o": [],
    "gpt-4.1": [],
    "gemini-3.1-pro-preview": [],
    "gemini-3-pro-preview": [],
    "gemini-2.5-flash": [],
    GRACE_MODEL_ID: [],
}
DEFAULT_TEMPERATURE_MAP = {
    "gpt-4o": 0,
    "gpt-4.1": 0,
    "gpt-5.4": 1.0,
    "gpt-5.5": 1.0,
    GRACE_MODEL_ID: 0.7,
}

# Context window policy (aligned with AutoFLUKA)
SOFT_CONTEXT_PROMPT_PERCENT = 55
RECOMMEND_SUMMARY_PERCENT = 70
AUTO_COMPRESS_PERCENT = 85

_DASH_DEPRECATION_MSG = (
    "The Dash runtime has been removed from RADIANT-LLM. "
    "Use the FastAPI backend in 'api.py' (or `radiant-llm-api`) with the React frontend."
)


def _raise_dash_deprecation() -> None:
    raise RuntimeError(_DASH_DEPRECATION_MSG)
DEFAULT_REASONING_EFFORT = "medium"

# Agent execution budget — hard backstops against runaway tool loops.
AGENT_MAX_ITERATIONS = 40
AGENT_MAX_EXECUTION_TIME_SECONDS = 600  # 10 min wall-clock backstop
AGENT_ITERATION_WARNING_MARGIN = 5      # inject a nudge marker once this many iterations remain

# LangChain's early-stopping sentinels — checked case-insensitively against
# the agent's raw "output" so a cap-out never reaches the user as a bare,
# unexplained string.
AGENT_STOPPED_MARKERS = (
    "agent stopped due to max iterations",
    "agent stopped due to iteration limit or time limit",
)

# -------------------------------
# Define the Chatbot class
# -------------------------------
class Chatbot:
    def __init__(self):
        self.model_choice = None
        self.llm_model = None
        self.llm = None
        self.embedding_model = None
        self.agent = None
        self.ai_assistant_agent = None
        self.selected_directory = None
        self.panels = []  # This list will hold conversation messages (as HTML Divs)
        # In-memory log of reasoning/agent events for streaming to UIs
        self.reasoning_events: List[str] = []

        # Token-budget memory policy: thresholds (warn 70%, compress 85%, hard_trim 95%)
        self.context_limit_tokens = 128_000
        self.context_usage_percent = 0.0

        # Inference params (decoupled from hardcode; UI can update via model-settings API)
        self.reasoning_effort = "medium"
        self.temperature = 1.0
        self.supported_reasoning_efforts: List[str] = []  # set per model in initialize_models
        self.skills_directory = ""
        self.auto_route_skills = True
        self.enabled_skill_ids: List[str] = []
        self.enabled_skill_modules: List[str] = list(RADIANT_AVAILABLE_SKILL_MODULES)
        self.enable_extended_skills = False
        self.skill_loading_level = "normal"
        preset = resolve_skill_loading_preset(self.skill_loading_level)
        self.skill_context_char_budget = preset["char_budget"]
        self.max_auto_routed_bundled = preset["max_auto_routed_bundled"]
        self.max_auto_routed_external = preset["max_auto_routed_external"]
        self.max_user_skill_file_bytes = preset["max_file_bytes"]
        self.skill_warnings: List[str] = []
        self.last_loaded_skills: List[Dict[str, Any]] = []
        self._last_skill_result = None  # SkillLoadResult from most recent build_skill_context call

        # Conversation memory: mirror AutoFLUKA behavior.
        self.memory = ConversationBufferWindowMemory(
            k=8,
            return_messages=True,
            memory_key="chat_history",
            input_key="input",
        )

        # Session persistence (Phase 2)
        self._session_dir: Optional[Path] = None
        self._active_session_id: Optional[str] = None
        self._active_session_meta: Optional[dict] = None
        # Summary of the session that was active before the current one; injected as
        # a cross-session preamble until the new session has grown its own history.
        self._prev_session_summary: Optional[str] = None
        # In-session memory summary note produced by "Summarize memory" button.
        # Prepended to every subsequent agent input without touching the JSONL file.
        self._memory_summary_note: Optional[str] = None
        self._last_summarize_ts: float = 0.0

    def reset_reasoning_events(self):
        """Clear the in-memory reasoning log for a fresh query."""
        # IMPORTANT: mutate in place so the ReasoningLoggerCallback,
        # which holds a reference to this list, continues to see updates.
        self.reasoning_events.clear()
        if getattr(self, "reasoning_cb", None) is not None:
            self.reasoning_cb.step_count = 0

    def update_context_usage(self, current_input: str = "", current_skill_context: str = ""):
        """Estimate context usage (system + memory + input) and set context_usage_percent."""
        try:
            vars = self.memory.load_memory_variables({})
            msgs = vars.get("chat_history", [])
            total_chars = sum(len(getattr(m, "content", str(m))) for m in msgs)
            total_chars += len(prompt_content) + len(current_input) + len(current_skill_context or "")
            estimated_tokens = total_chars // 4  # rough; use tiktoken for accuracy
            self.context_usage_percent = min(
                100.0,
                (estimated_tokens / self.context_limit_tokens) * 100,
            )
        except Exception:
            self.context_usage_percent = 0.0

    def get_context_usage_payload(self, current_input: str = "") -> Dict[str, Any]:
        self.update_context_usage(current_input)
        usage_percent = float(self.context_usage_percent or 0.0)
        messages = list(getattr(self.memory.chat_memory, "messages", []) or [])
        soft_prompt = usage_percent >= SOFT_CONTEXT_PROMPT_PERCENT
        recommend_summary = usage_percent >= RECOMMEND_SUMMARY_PERCENT
        auto_compress = usage_percent >= AUTO_COMPRESS_PERCENT

        if auto_compress:
            status = "critical"
            advice = (
                "Context is near the hard limit. Summarize memory now; "
                "RADIANT-LLM will auto-compress before large requests when enabled."
            )
        elif recommend_summary:
            status = "high"
            advice = "Context is getting full. Summarizing memory before the next large request is recommended."
        elif soft_prompt:
            status = "elevated"
            advice = "Context is growing. You can summarize memory early to preserve room for future turns."
        else:
            status = "healthy"
            advice = "Context usage is healthy."

        return {
            "usage_percent": round(usage_percent, 1),
            "limit_tokens": getattr(self, "context_limit_tokens", 128_000) or 128_000,
            "warn": recommend_summary,
            "compress": auto_compress,
            "soft_prompt": soft_prompt,
            "recommend_summary": recommend_summary,
            "auto_compress": auto_compress,
            "status": status,
            "advice": advice,
            "can_summarize": len(messages) > 2,
            "memory_messages": len(messages),
        }

    def _message_role_label(self, message: Any) -> str:
        msg_type = str(
            getattr(message, "type", "")
            or getattr(message, "__class__", type(message)).__name__
        ).lower()
        if "human" in msg_type:
            return "User"
        if "ai" in msg_type:
            return "Assistant"
        if "system" in msg_type:
            return "System"
        return "Message"

    def _build_summary_source_text(self, messages: List[Any], max_chars: int = 60_000) -> str:
        if not messages:
            return ""

        remaining = max_chars
        selected_blocks: List[str] = []
        truncated = False

        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "").strip()
            if not content:
                continue
            block = f"{self._message_role_label(message)}: {content}"
            if len(block) + 2 > remaining:
                truncated = True
                continue
            selected_blocks.append(block)
            remaining -= len(block) + 2

        selected_blocks.reverse()
        transcript = "\n\n".join(selected_blocks)
        if truncated:
            transcript = (
                "[Earlier chat history was partially omitted to fit the summarization budget.]\n\n"
                + transcript
            )
        return transcript

    def summarize_chat_history(self, keep_last_turns: int = 2) -> Dict[str, Any]:
        if not self.llm_model:
            return {
                "status": "error",
                "message": "No model initialized. Initialize a model before summarizing memory.",
            }

        # Cooldown guard: skip LLM call if summarized within the last 30 seconds.
        if self._memory_summary_note and (time.time() - self._last_summarize_ts) < 30:
            return {
                "status": "noop",
                "message": "Memory was summarized recently. Chat more before summarizing again.",
                "summary": self._memory_summary_note,
                "context_usage": self.get_context_usage_payload(),
            }

        all_messages = list(getattr(self.memory.chat_memory, "messages", []) or [])
        if not all_messages:
            self.context_usage_percent = 0.0
            return {
                "status": "noop",
                "message": "No conversation memory is stored yet.",
                "context_usage": self.get_context_usage_payload(),
            }

        keep_last_turns = max(0, int(keep_last_turns))
        keep_last_messages = keep_last_turns * 2
        if keep_last_messages <= 0:
            older_messages = all_messages
            kept_messages: List[Any] = []
        else:
            older_messages = all_messages[:-keep_last_messages]
            kept_messages = all_messages[-keep_last_messages:]

        if len(older_messages) < 2:
            return {
                "status": "noop",
                "message": "Not enough older conversation history to summarize yet.",
                "context_usage": self.get_context_usage_payload(),
            }

        summary_source = self._build_summary_source_text(older_messages)
        if not summary_source:
            return {
                "status": "noop",
                "message": "Conversation history is too small to summarize meaningfully.",
                "context_usage": self.get_context_usage_payload(),
            }

        summarize_system_prompt = (
            "You are compressing prior chat history for a nuclear engineering educational assistant. "
            "Produce a compact memory note that preserves the user's goals, files/directories in use, "
            "important technical decisions, concrete outputs already produced, unresolved questions, "
            "and any constraints that must carry forward. Do not include hidden prompts, chain-of-thought, "
            "or internal policy text. Keep it concise and structured with short bullets."
        )
        summarize_user_prompt = (
            "Summarize the following older conversation history so it can replace those older turns in memory.\n\n"
            f"{summary_source}"
        )

        try:
            summary_result = self.llm_model.invoke(
                [
                    SystemMessage(content=summarize_system_prompt),
                    HumanMessage(content=summarize_user_prompt),
                ]
            )
        except Exception as err:
            return {
                "status": "error",
                "message": f"Memory summarization failed: {err}",
                "context_usage": self.get_context_usage_payload(),
            }

        summary_text = self._normalize_agent_output(
            getattr(summary_result, "content", summary_result)
        )
        if not summary_text:
            return {
                "status": "error",
                "message": "Memory summarization returned an empty summary.",
                "context_usage": self.get_context_usage_payload(),
            }

        # Store summary for in-session agent injection — JSONL file is NEVER touched.
        self._memory_summary_note = f"Conversation memory summary:\n{summary_text}"
        self._last_summarize_ts = time.time()

        # Shrink the agent's visible window so the % drops (file remains intact).
        if hasattr(self.memory.chat_memory, "_keep"):
            self.memory.chat_memory._keep = max(1, keep_last_turns)

        # Persist summary to index.json for cross-session retrieval (Piece 2).
        try:
            from utils.session_store import upsert_index
            sd = self._get_session_dir()
            if self._active_session_meta:
                entry = dict(self._active_session_meta)
                entry["summary"] = summary_text
                upsert_index(sd, entry)
        except Exception:
            pass

        usage_payload = self.get_context_usage_payload()
        return {
            "status": "ok",
            "message": (
                f"Conversation memory summarized. Older turns compressed into a memory note; "
                f"the last {keep_last_turns} turns remain in the active window."
            ),
            "summary": summary_text,
            "context_usage": usage_payload,
        }

    def get_skill_settings_payload(self) -> Dict[str, Any]:
        catalog = discover_radiant_skill_catalog(
            app_root=Path(__file__).resolve().parent,
            skills_root_override=self.skills_directory or None,
            max_file_bytes=self.max_user_skill_file_bytes,
        )
        warnings = list(catalog.get("warnings", []))
        available_ids = {
            skill["id"]
            for skill in [*catalog.get("bundled_skills", []), *catalog.get("external_skills", [])]
        }
        missing = [skill_id for skill_id in self.enabled_skill_ids if skill_id not in available_ids]
        if missing:
            warnings.extend(
                f"Configured skill `{skill_id}` is not currently available." for skill_id in missing
            )
        self.skill_warnings = warnings
        app_root = Path(__file__).resolve().parent
        bundled_root = resolve_bundled_skills_root(app_root)
        return {
            "skills_directory": self.skills_directory,
            "auto_route_skills": bool(self.auto_route_skills),
            "enabled_skill_ids": list(self.enabled_skill_ids),
            "enabled_modules": list(self.enabled_skill_modules),
            "enable_extended_skills": bool(self.enable_extended_skills),
            "skill_loading_level": self.skill_loading_level,
            "char_budget": self.skill_context_char_budget,
            "available_modules": radiant_skill_catalog_entries(
                app_root,
                max_file_bytes=self.max_user_skill_file_bytes,
            ),
            "bundled_skills_path": str(bundled_root),
            "available_bundled_skills": catalog.get("bundled_skills", []),
            "available_external_skills": catalog.get("external_skills", []),
            "warnings": warnings,
        }

    def set_skill_settings(
        self,
        skills_directory: str = "",
        auto_route_skills: bool = True,
        enabled_skill_ids: Sequence[str] | None = None,
        enabled_modules: Sequence[str] | None = None,
        enable_extended_skills: bool | None = None,
        skill_loading_level: str | None = None,
    ):
        prev_directory = self.skills_directory
        self.skills_directory = (skills_directory or "").strip()
        self.auto_route_skills = bool(auto_route_skills)
        # Auto-route only (AutoFLUKA/AutoSAM parity); UI does not expose manual skill pins.
        self.enabled_skill_ids = []
        if enabled_modules is not None:
            self.enabled_skill_modules = [
                str(module_id).strip().lower().replace("_", "-")
                for module_id in enabled_modules
                if str(module_id).strip()
            ]
        if enable_extended_skills is not None:
            self.enable_extended_skills = bool(enable_extended_skills)
        if skill_loading_level is not None and skill_loading_level in SKILL_LOADING_PRESETS:
            self.skill_loading_level = skill_loading_level
            preset = resolve_skill_loading_preset(self.skill_loading_level)
            self.skill_context_char_budget = preset["char_budget"]
            self.max_auto_routed_bundled = preset["max_auto_routed_bundled"]
            self.max_auto_routed_external = preset["max_auto_routed_external"]
            self.max_user_skill_file_bytes = preset["max_file_bytes"]
        # Run Tier A scan (sync) + kick off Tier B (background) when directory changes.
        if self.skills_directory and self.skills_directory != prev_directory:
            scan_user_skills_directory(self.skills_directory)
            self._trigger_tier_b_scans_bg()
        payload = self.get_skill_settings_payload()
        mode = "on" if self.auto_route_skills else "off"
        module_count = len(self.enabled_skill_modules)
        external_skill_count = len(payload.get("available_external_skills", []))
        warning_count = len(payload.get("warnings", []))
        summary_parts = [
            "Skills ready",
            f"auto-routing {mode}",
            f"{module_count} bundled module(s)",
            f"{external_skill_count} external pack(s)",
            f"loading level: {self.skill_loading_level}",
        ]
        if warning_count:
            summary_parts.append(f"warnings {warning_count}")
        return dbc.Alert(". ".join(summary_parts) + ".", color="info")

    def get_llm_fn(self):
        """Return a callable(system, user) -> str adapter around the initialized LLM."""
        llm = self.llm_model

        def _call(system_prompt: str, user_msg: str) -> str:
            from langchain_core.messages import HumanMessage, SystemMessage
            response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_msg)])
            content = getattr(response, "content", response)
            # Normalize list-of-blocks (newer OpenAI/LangChain response format).
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict):
                        parts.append(block.get("text", ""))
                    else:
                        parts.append(str(getattr(block, "text", block)))
                content = "".join(parts)
            return str(content)

        return _call

    def _trigger_tier_b_scans_bg(self) -> None:
        """Launch Tier B LLM scans for all tier-a-safe external packs in a daemon thread."""
        import threading as _threading
        if not self.skills_directory or self.llm_model is None:
            return
        skills_dir = self.skills_directory
        llm_fn = self.get_llm_fn()
        model_id = str(self.model_choice or "")

        def _run():
            from utils.skill_scan_cache import (
                content_hash as _chash,
                get_entry as _get,
                load_cache as _load,
                put_entry as _put,
                save_cache as _save,
            )
            from utils.skill_security import scan_skill_full as _scan

            cache = _load(skills_dir)
            updated = False
            for child in sorted(Path(skills_dir).iterdir()):
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue
                try:
                    body = skill_md.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                chash = _chash(body)
                entry = _get(cache, child.name, chash)
                # Skip packs already fully scanned or already blocked by Tier A.
                if entry and entry.get("tier") == "A+B":
                    continue
                if entry and not entry.get("safe", True):
                    continue
                result = _scan(child.name, body, llm_fn=llm_fn, model_id=model_id)
                _put(cache, child.name, chash, {
                    "safe": result.safe,
                    "reason": result.reason,
                    "confidence": result.confidence,
                    "tier": result.tier,
                    "model_used": result.model_used,
                })
                updated = True
            if updated:
                _save(cache, skills_dir)

        t = _threading.Thread(target=_run, daemon=True)
        t.start()

    def scan_skills(self) -> Tuple[List[Any], List[str]]:
        """Tier A sync scan of the current user skills directory; triggers Tier B in background."""
        if not self.skills_directory:
            return [], []
        results, warnings = scan_user_skills_directory(
            self.skills_directory,
            llm_fn=self.get_llm_fn() if self.llm_model is not None else None,
            model_id=str(self.model_choice or ""),
        )
        self._trigger_tier_b_scans_bg()
        return results, warnings

    def build_skill_context(self, query: str) -> str:
        try:
            enabled_modules_arg: List[str] | None
            if len(self.enabled_skill_modules) == len(RADIANT_AVAILABLE_SKILL_MODULES):
                enabled_modules_arg = None
            else:
                enabled_modules_arg = list(self.enabled_skill_modules)
            result: SkillLoadResult = build_radiant_skill_context_with_meta(
                query=query,
                app_root=Path(__file__).resolve().parent,
                skills_root_override=self.skills_directory or None,
                auto_route_skills=self.auto_route_skills,
                enabled_skill_ids=self.enabled_skill_ids,
                enabled_modules=enabled_modules_arg,
                enable_extended_skills=self.enable_extended_skills,
                char_budget=self.skill_context_char_budget,
                max_file_bytes=self.max_user_skill_file_bytes,
                max_auto_routed_bundled=self.max_auto_routed_bundled,
                max_auto_routed_external=self.max_auto_routed_external,
            )
        except Exception as exc:
            self.skill_warnings = [f"Skill context injection failed: {exc}"]
            self.last_loaded_skills = []
            self._last_skill_result = None
            return ""
        self.skill_warnings = result.warnings
        self.last_loaded_skills = result.selected_skills
        self._last_skill_result = result
        return result.context

    def apply_inference_params(self, reasoning_effort: str = None, temperature: float = None) -> None:
        """
        Update reasoning_effort and/or temperature. Mutates the LLM in place when possible.
        Call after model is initialized. Validates reasoning_effort against supported list.
        """
        if reasoning_effort is not None:
            if self.supported_reasoning_efforts and reasoning_effort not in self.supported_reasoning_efforts:
                raise ValueError(
                    f"Reasoning effort '{reasoning_effort}' not supported for {self.model_choice}. "
                    f"Supported: {self.supported_reasoning_efforts}"
                )
            self.reasoning_effort = reasoning_effort

        if temperature is not None:
            # GPT‑5.x families have a fixed effective temperature of 1.0; ignore UI changes.
            if self.model_choice and str(self.model_choice).startswith("gpt-5"):
                self.temperature = 1.0
            else:
                self.temperature = float(max(0.0, min(2.0, temperature)))

        # Mutate LLM in place (OpenAI ChatOpenAI supports these)
        if self.llm_model is not None:
            if hasattr(self.llm_model, "temperature"):
                self.llm_model.temperature = self.temperature
            if hasattr(self.llm_model, "reasoning_effort") and self.supported_reasoning_efforts:
                self.llm_model.reasoning_effort = self.reasoning_effort
            elif hasattr(self.llm_model, "model_kwargs") and self.supported_reasoning_efforts:
                self.llm_model.model_kwargs = getattr(self.llm_model, "model_kwargs", {}) or {}
                self.llm_model.model_kwargs["reasoning_effort"] = self.reasoning_effort

    def _normalize_agent_output(self, output: Any) -> str:
        """
        Normalize agent output to plain text.
        Handles string outputs and Responses-API style structured blocks.
        """
        if output is None:
            return ""

        if isinstance(output, str):
            return output

        # LangChain message-like objects often carry content as str | list[dict].
        if hasattr(output, "content") and not isinstance(output, (dict, list)):
            try:
                content = getattr(output, "content")
                if content is not None:
                    return self._normalize_agent_output(content)
            except Exception:
                pass

        if isinstance(output, dict):
            txt = output.get("text")
            if txt is not None:
                return self._normalize_agent_output(txt)
            content = output.get("content")
            if content is not None:
                return self._normalize_agent_output(content)
            nested_output = output.get("output")
            if nested_output is not None:
                return self._normalize_agent_output(nested_output)
            return json.dumps(output, ensure_ascii=False)

        if isinstance(output, list):
            parts: List[str] = []
            for item in output:
                if isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type == "text" and item.get("text") is not None:
                        normalized = self._normalize_agent_output(item.get("text"))
                        if normalized.strip():
                            parts.append(normalized)
                        continue
                    txt = item.get("text")
                    if txt is not None:
                        normalized = self._normalize_agent_output(txt)
                        if normalized.strip():
                            parts.append(normalized)
                        continue
                    content = item.get("content")
                    if content is not None:
                        normalized = self._normalize_agent_output(content)
                        if normalized.strip():
                            parts.append(normalized)
                        continue
                elif isinstance(item, str) and item.strip():
                    parts.append(item)
                else:
                    normalized = self._normalize_agent_output(item)
                    if normalized.strip():
                        parts.append(normalized)
            merged = "\n".join(p for p in parts if p).strip()
            if merged:
                return merged
            return json.dumps(output, ensure_ascii=False)

        return str(output)

    def _extract_display_response(self, raw_text: Any) -> str:
        normalized = raw_text if isinstance(raw_text, str) else self._normalize_agent_output(raw_text)
        if not isinstance(normalized, str):
            normalized = self._normalize_agent_output(normalized)
        if not normalized:
            return ""
        marker = re.search(r"Final Answer:?\s*", normalized, flags=re.IGNORECASE)
        if marker:
            tail = normalized[marker.end():].strip()
            if tail:
                return tail
        return normalized.strip()

    def _synthesize_cap_out_summary(self, intermediate_steps: List[Any]) -> str:
        """One no-tool LLM call over the captured (action, observation) pairs from
        a cap-out agent run, producing a real status report instead of LangChain's
        bare 'Agent stopped due to max iterations.' sentinel. Falls back to a raw
        steps dump if the synthesis call itself fails."""
        if not intermediate_steps:
            return (
                "The agent reached its step/time budget before taking any recorded "
                "action. Try narrowing the request or splitting it into smaller steps."
            )

        lines = []
        for action, observation in intermediate_steps:
            tool_name = getattr(action, "tool", "unknown_tool")
            tool_input = getattr(action, "tool_input", "")
            obs_text = observation if isinstance(observation, str) else str(observation)
            obs_text = obs_text[:800]
            lines.append(f"- Tool: {tool_name}\n  Input: {tool_input}\n  Observation: {obs_text}")
        steps_text = "\n".join(lines)

        try:
            summary_result = self.llm_model.invoke(
                [
                    SystemMessage(content=(
                        "You are summarizing an AI agent's run that was cut off after hitting "
                        "its step/time budget before it could produce a Final Answer. Given the "
                        "list of tool calls and their observations below, write a concise status "
                        "report covering: what was tried, what was found so far, what is still "
                        "pending, and a suggested next step for the user. Do not apologize or "
                        "explain what an iteration budget is — just report the findings."
                    )),
                    HumanMessage(content=f"Tool calls made before the cutoff:\n\n{steps_text}"),
                ]
            )
            summary_text = self._normalize_agent_output(
                getattr(summary_result, "content", summary_result)
            )
            if summary_text:
                return (
                    "The agent reached its step/time budget before finishing. "
                    f"Here is a summary of what was done:\n\n{summary_text}"
                )
        except Exception:
            pass

        # Fallback: raw steps dump if synthesis itself failed.
        return (
            "The agent reached its step/time budget before finishing. "
            f"Steps taken so far:\n\n{steps_text}"
        )

    def _invoke_agent_and_recover(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Wraps self.ai_assistant_agent.invoke(...). If the result's raw output is
        LangChain's bare cap-out sentinel, replace it with a synthesized summary of
        the captured intermediate_steps instead of surfacing the unexplained string."""
        result = self.ai_assistant_agent.invoke(payload)
        raw_output = result.get("output", "")
        output_text = raw_output if isinstance(raw_output, str) else str(raw_output)
        if any(marker in output_text.lower() for marker in AGENT_STOPPED_MARKERS):
            intermediate_steps = result.get("intermediate_steps", [])
            result = dict(result)
            result["output"] = self._synthesize_cap_out_summary(intermediate_steps)
        return result

    def _wrap_tools_with_iteration_budget(self, agent_tools):
        """Wrap each tool's .func so that, once few iterations remain, the tool's
        return value carries a short nudge telling the model to wrap up. Every
        tool here is a plain @tool StructuredTool with a .func attribute — no
        async tools, no BaseTool subclasses — so copy.copy + reassigning .func
        is structurally safe. Non-str/dict returns (e.g. URLValidationTool's
        bool, FileDownloaderTool's tuple) pass through untouched."""
        wrapped = []
        for t in agent_tools:
            original_func = t.func

            def _budget_aware(*args, _orig=original_func, **kwargs):
                result = _orig(*args, **kwargs)
                remaining = AGENT_MAX_ITERATIONS - self.reasoning_cb.step_count
                return apply_iteration_marker(result, remaining, AGENT_ITERATION_WARNING_MARGIN)

            new_tool = copy.copy(t)
            new_tool.func = _budget_aware
            wrapped.append(new_tool)
        return wrapped

    def initialize_models(self, model_choice):
        if not model_choice:
            return [dbc.Alert("Please select a model before initializing.", color="danger")]

        self.model_choice = model_choice
        self.agent_tools = list(tools) + [make_search_past_sessions_tool(self)]
        # 1) create model‐specific logger
        self.logger = LLMReasoningChainLogger(model_choice)
        self.logger.info(f"\n============Initializing The Model: '{model_choice}'=======================")

        # 2) build callback manager for reasoning steps
        #    also stream the reasoning steps into self.reasoning_events
        self.reasoning_cb = ReasoningLoggerCallback(self.logger, events_sink=self.reasoning_events)
        cb_manager  = CallbackManager([self.reasoning_cb])
           
        # Initialize the alerts bucket 
        alerts = []
        supported_models = {
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.3-chat-latest",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
            "gpt-4.1",
            "gpt-4o",
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-2.5-flash",
            GRACE_MODEL_ID,
            # "o1",
            # "o3-mini",
            # "o3",
        }
        if self.model_choice not in supported_models:
            return [
                dbc.Alert(
                    f"Unsupported model '{self.model_choice}'. "
                    "Please select a GPT, Gemini, or Grace model.",
                    color="danger",
                )
            ]

        # Set inference params from model-specific defaults (UI can override later)
        self.supported_reasoning_efforts = REASONING_EFFORT_MAP.get(self.model_choice, [])
        if self.supported_reasoning_efforts:
            if self.reasoning_effort not in self.supported_reasoning_efforts:
                self.reasoning_effort = (
                    "medium"
                    if "medium" in self.supported_reasoning_efforts
                    else self.supported_reasoning_efforts[0]
                )
        else:
            self.reasoning_effort = "medium"
        self.temperature = DEFAULT_TEMPERATURE_MAP.get(self.model_choice, 1.0)

         #-----------------------------------------------------------------------------------------------------
        # gpt-5 = "gpt-5-2025-08-07"
        # gpt-5.1 = "gpt-5-2025-08-07"

        if self.model_choice in [
            "gpt-5",
            "gpt-5.1",
            "gpt-5.2",
            "gpt-5.3-chat-latest",
            "gpt-5.4",
            "gpt-5.5",
        ]:
            gpt_model = self.model_choice  # dropdown value
            print(f"Model selected: {gpt_model}")

            gpt_params = {
                "model": gpt_model,
                "temperature": self.temperature,
                "api_key": openai_key,
                "streaming": True,
            }
            if self.supported_reasoning_efforts:
                gpt_params["reasoning_effort"] = self.reasoning_effort
            if gpt_model in {"gpt-5.4", "gpt-5.5"}:
                gpt_params["use_responses_api"] = True
                gpt_params["stream_usage"] = True
            self.llm_model = ChatOpenAI(**gpt_params)
            print(f"Initialized llm_model: {self.llm_model}")

            # For image analysis
            self.gpt_vision_llm = gpt_model
            self.gpt_vision_client = openai.Client(api_key=openai_key)    
                
            # Set the PromptTemplate
            self.prompt = ChatPromptTemplate.from_messages([
                MessagesPlaceholder(variable_name="chat_history"),
                ("system", prompt_content),
                ("system", "{skill_context}"),
                ("system", "{runtime_context}"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}")
            ])
            # alerts.append(dbc.Alert(f"{self.model_choice} selected.", color="success"))

            # Initialize the Embeddings
            self.llm_type = "gpt"
            self.embedding_model = OpenAIEmbeddings(openai_api_key=openai_key)
            self.persistent_dir = f"{self.llm_type}_vector_store"

            #-----------------------------------------------------------------------------------------------------
        if self.model_choice in [
            "gpt-4o",
            "gpt-4.1",
            # "o1",
            # "o3-mini",
            # "o3",
        ]:
            gpt_model = self.model_choice  # dropdown value
            print(f"Model selected: {gpt_model}")
            self.supported_reasoning_efforts = []  # non-reasoning models

            gpt_params = {
                "model": gpt_model,
                "temperature": self.temperature,
                "api_key": openai_key,
                "streaming": True,
            }

            print(f"Instantiating model {gpt_model} with params: {gpt_params}")
            self.llm_model = ChatOpenAI(**gpt_params)
            print(f"Initialized llm_model: {self.llm_model}")

            # For image analysis
            self.gpt_vision_llm = gpt_model
            self.gpt_vision_client = openai.Client(api_key=openai_key)    
                
            # Set the PromptTemplate
            self.prompt = ChatPromptTemplate.from_messages([
                MessagesPlaceholder(variable_name="chat_history"),
                ("system", prompt_content),
                ("system", "{skill_context}"),
                ("system", "{runtime_context}"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}")
            ])
            # alerts.append(dbc.Alert(f"{self.model_choice} selected.", color="success"))

            # Initialize the Embeddings
            self.llm_type = "gpt"
            self.embedding_model = OpenAIEmbeddings(openai_api_key=openai_key)
            self.persistent_dir = f"{self.llm_type}_vector_store"
            print(f"\nInitialized embeddings:\n {self.embedding_model}")
        #-----------------------------------------------------------------------------------------------------
        #-----------------------------------------------------------------------------------------------------
        elif self.model_choice in ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-2.5-flash"]: # Only if ig have Paid API for "gemini-3"
            gemini_model = self.model_choice  # dropdown value
            print(f"Model selected: {gemini_model}")

            # Define base params
            gemini_params = {
                "model": gemini_model,
                "api_key": gemini_key,
                "stream_usage": True
            }
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", prompt_content),
                ("system", "{skill_context}"),
                ("system", "{runtime_context}"),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}")
            ])
            self.llm_type = "gemini"
            self.llm_model = ChatGoogleGenerativeAI(**gemini_params)

            # For image analysis: Only tyhe model required
            self.gemini_vision_llm = genai.GenerativeModel(gemini_model)    

            self.embedding_model = GoogleGenerativeAIEmbeddings(model="models/embedding-001", google_api_key=gemini_key)
            self.persistent_dir = f"{self.llm_type}_vector_store"
            print(f"\nInitialized embeddings:\n {self.embedding_model}")

        elif self.model_choice == GRACE_MODEL_ID:
            print(f"Model selected: {GRACE_MODEL_ID}")
            try:
                vllm_cfg = load_vllm_config()
            except ValueError as exc:
                return [dbc.Alert(str(exc), color="danger")]

            ok, msg, models_json = check_vllm_reachable(vllm_cfg)
            if not ok:
                return [dbc.Alert(msg, color="danger")]

            grace_ctx = get_vllm_max_model_len(vllm_cfg, models_json)
            self.context_limit_tokens = grace_ctx
            self.skill_context_char_budget = grace_skill_context_char_budget(grace_ctx)
            grace_system_prompt = grace_system_prompt_for_context(prompt_content, grace_ctx)
            self.agent_tools = list(grace_tools(tools)) + [make_search_past_sessions_tool(self)]

            self.llm_model = build_grace_chat_openai(
                temperature=self.temperature,
                streaming=True,
                config=vllm_cfg,
            )
            print(f"Initialized llm_model (Grace vLLM): {self.llm_model}")

            self.prompt = ChatPromptTemplate.from_messages([
                MessagesPlaceholder(variable_name="chat_history"),
                ("system", grace_system_prompt),
                ("system", "{skill_context}"),
                ("system", "{runtime_context}"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}"),
            ])

            embed_provider = (os.getenv("RADIANT_EMBEDDING_PROVIDER") or "openai").strip().lower()
            if embed_provider == "openai":
                if not openai_key:
                    return [
                        dbc.Alert(
                            "RADIANT_EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set.",
                            color="danger",
                        )
                    ]
                self.llm_type = "gpt"
                self.embedding_model = OpenAIEmbeddings(openai_api_key=openai_key)
                self.persistent_dir = f"{self.llm_type}_vector_store"
                self.gpt_vision_llm = "gpt-4o"
                self.gpt_vision_client = openai.Client(api_key=openai_key)
            elif embed_provider == "gemini":
                if not gemini_key:
                    return [
                        dbc.Alert(
                            "RADIANT_EMBEDDING_PROVIDER=gemini but GEMINI_API_KEY is not set.",
                            color="danger",
                        )
                    ]
                self.llm_type = "gemini"
                self.embedding_model = GoogleGenerativeAIEmbeddings(
                    model="models/embedding-001",
                    google_api_key=gemini_key,
                )
                self.persistent_dir = f"{self.llm_type}_vector_store"
                self.gemini_vision_llm = genai.GenerativeModel("gemini-2.5-flash")
            elif embed_provider == "local":
                self.llm_type = "local"
                try:
                    self.embedding_model = build_local_embeddings()
                except (ImportError, ValueError) as exc:
                    return [dbc.Alert(str(exc), color="danger")]
                self.persistent_dir = "local_vector_store"
                # Optional: cloud vision only if ingesting new PDFs from RADIANT (JSONL already built is fine)
                if openai_key:
                    self.gpt_vision_llm = "gpt-4o"
                    self.gpt_vision_client = openai.Client(api_key=openai_key)
                elif gemini_key:
                    self.gemini_vision_llm = genai.GenerativeModel("gemini-2.5-flash")
            else:
                return [
                    dbc.Alert(
                        f"Invalid RADIANT_EMBEDDING_PROVIDER={embed_provider!r}. "
                        "Use 'openai', 'gemini', or 'local'.",
                        color="danger",
                    )
                ]

            print(f"Initialized embeddings ({embed_provider}): {self.embedding_model}")
            embed_detail = embed_provider
            if embed_provider == "local":
                embed_detail = (
                    f"local {os.getenv('RADIANT_LOCAL_EMBEDDING_MODEL', DEFAULT_LOCAL_EMBEDDING_MODEL)} "
                    f"({DEFAULT_LOCAL_EMBEDDING_DIM}-dim → local_vector_store)"
                )
            skill_cap_msg = (
                "bundled skill maps omitted for this context limit"
                if self.skill_context_char_budget == 0
                else f"skill injection capped at {self.skill_context_char_budget:,} chars"
            )
            alerts.append(
                dbc.Alert(
                    f"Grace Gemma chat via vLLM (context limit {grace_ctx:,} tokens from server). "
                    f"RAG embeddings: {embed_detail}. "
                    f"Compact agent mode: {len(grace_system_prompt):,}-char system prompt, "
                    f"{len(self.agent_tools)}/{len(tools)} tools, {skill_cap_msg}.",
                    color="info",
                )
            )
            if grace_ctx <= 5120:
                alerts.append(
                    dbc.Alert(
                        "Grace context is very small; RADIANT uses a shortened system prompt and "
                        "fewer tools so short chat works (unlike the full cloud agent). "
                        "For PDF/CSV-heavy workflows use cloud models or raise --max-model-len if GPUs allow.",
                        color="warning",
                    )
                )
            if embed_provider == "local":
                alerts.append(
                    dbc.Alert(
                        "Local embeddings: rebuild Chroma with rebuild_vector_store after switching "
                        "from OpenAI/Gemini indexes. New PDF→JSONL ingest from RADIANT still needs "
                        "OPENAI_API_KEY or GEMINI_API_KEY for vision unless JSONL KBs already exist.",
                        color="warning",
                    )
                )
            tools_ok, tools_hint = check_vllm_native_tool_support(vllm_cfg)
            if not tools_ok and tools_hint:
                alerts.append(
                    dbc.Alert(
                        "RADIANT tools (native tool calling) are not supported by this vLLM job: "
                        + tools_hint,
                        color="warning",
                    )
                )

        if self.llm_model is None:
            return [
                dbc.Alert(
                    f"Model '{self.model_choice}' was not initialized. Check configuration.",
                    color="danger",
                )
            ]

        #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
        # Create An OpenAI tool Calling Agent: Different from ReAct
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
        # Create the tool-calling agent and the assistant agent
        self.agent_tools = self._wrap_tools_with_iteration_budget(self.agent_tools)
        self.runtime_context = format_runtime_context(AGENT_MAX_ITERATIONS, AGENT_MAX_EXECUTION_TIME_SECONDS)
        self.agent = create_tool_calling_agent(self.llm_model, self.agent_tools, self.prompt)
        self.ai_assistant_agent = AgentExecutor(agent=self.agent,
                                                tools=self.agent_tools,
                                                handle_parsing_errors=True,
                                                verbose=False,
                                                max_iterations=AGENT_MAX_ITERATIONS,
                                                max_execution_time=AGENT_MAX_EXECUTION_TIME_SECONDS,
                                                memory=self.memory,
                                                callback_manager=cb_manager,          # ← this wires in *all* the event hooks
                                                return_intermediate_steps=True        # ← and also lets you pull out the step list
                                                )
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++   
         # Inform the user about the working directory
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
        if self.selected_directory:
            # alerts.append(dbc.Alert(f"Using working directory: {self.selected_directory}", color="info",
            #                         style={ "whiteSpace": "normal", "wordBreak": "break-word",
            #                                 "overflowWrap": "break-word", "maxWidth": "300px" }))
            # Just print, do not send a message alert
            print(f"Working directory '{self.selected_directory}' initialized")
        else:
            print(f"No working directory selected. Using the default temporary directory: '{temp_dir}'.")
            # alerts.append(dbc.Alert(f"No working directory selected. Using the default temporary directory: '{temp_dir}'.", color="warning",
            #               style={ "whiteSpace": "normal", "wordBreak": "break-word", "overflowWrap": "break-word", "maxWidth": "300px" }))
        alerts.append(dbc.Alert(f"'{self.model_choice}' model initialized successfully.", color="success"))
        # Trigger Tier B background scans now that a model is available.
        self._trigger_tier_b_scans_bg()
        return alerts
    #-----------------------------------------------------------------------------------------------------------------------------------------
    def set_directory(self, directory):
        alerts = []
        if os.path.isdir(directory):
            self.selected_directory = directory
            # alerts.append(dbc.Alert(f"Using working directory: {self.selected_directory}", color="info",
            #                         style={ "whiteSpace": "normal", "wordBreak": "break-word",
            #                                 "overflowWrap": "break-word", "maxWidth": "300px" }))
            alerts.append(dbc.Alert(f"Working directory successfully instantiated", color="info",
                                    style={ "whiteSpace": "normal", "wordBreak": "break-word",
                                            "overflowWrap": "break-word", "maxWidth": "300px" }))
            
            # alerts.append(dbc.Alert(f"{self.model_choice} model initialized successfully.", color="success"))
            
        else:
           # Do not print any directory paths (sensitive).
           alerts.append(dbc.Alert("Invalid directory. Please provide a valid path.", color="danger"))

        return alerts

    #-----------------------------------------------------------------------------------------------------------------------------------------

    def convchain(self, query):
        if not query:
            return []
        # Ensure that models and the assistant agent have been initialized
        if not self.llm_model or not self.embedding_model or not self.ai_assistant_agent:
            return [dbc.Alert("No model initialized. Please initialize llm model before querying.", color="danger")]
        # Build one single “input” string that includes the directory hint
        if self.selected_directory:
            global_directory_message = f" | Working directory: {self.selected_directory}"
        else:
            global_directory_message = f" | No working directory provided"
        query_with_directory = query.strip() + global_directory_message 

        # Now the agent only needs “input”        
        # tool_query = url_preservation_for_websearch(query_with_directory)
        tool_query = query_with_directory

        # Log reasoning before calling the agent
        self.logger.info("\n============================NEW QUERY============================")
        self.logger.debug(f"{tool_query}")

        # # Log reasoning before calling the agent
        # self.logger.debug(f"{tool_query}")

        # reset reasoning log for this query
        self.reset_reasoning_events()

        skill_context = self.build_skill_context(query)
        _sr = self._last_skill_result
        if _sr and (_sr.loaded_bundled_count + _sr.loaded_user_count) > 0:
            for label in _sr.loaded_labels:
                self.reasoning_events.append(label)
        elif skill_context:
            self.reasoning_events.append("[SYSTEM] Global skill policy injected.")
        for msg in (self.skill_warnings[:5] if not _sr else [w for w in self.skill_warnings[:5] if "blocked" in w.lower() or "warning" in w.lower()]):
            self.reasoning_events.append(f"[SKILL WARNING] {msg}")

        # invoke the chain
        result = self.ai_assistant_agent.invoke({
                                                "input": tool_query,
                                                "global_directory_message": global_directory_message,
                                                "skill_context": skill_context,
                                                "runtime_context": self.runtime_context,
                                               })
        #------------------------------------------
        # Safely log each intermediate step
        # try:
        #     for step in result.get("intermediate_steps", []):
        #         self.logger.debug(f"[STEP] {step}")

            
        # except Exception as e:
        #     self.logger.error(f"Error logging intermediate_steps: {e}")

        # Now that all steps are logged, mark the end
        # self.logger.info("\n============================END OF QUERY============================")
        #------------------------------------------

        raw_output = result.get("output", "")
        response = self._normalize_agent_output(raw_output)
        display_response = self._extract_display_response(response)
        display_response = sanitize_user_markdown(display_response)

        # Now, use `display_response` to render in the GUI.

        display_response_md = MarkdownParser(display_response)
        display_response_md = sanitize_user_markdown(display_response_md)
        FormattedResponse = dcc.Markdown(display_response_md, 
                                         className="markdown-body",
                                         link_target="_blank")  
        self.panels.append(html.Div([
            html.P([html.B("Query:"), " ", html.Span(query, style={"color": "blue"})]),
            html.P([html.B("RadiantLLM:"), " ", FormattedResponse], style={"backgroundColor": "#F6F6F6"}),
            html.Hr()
        ]))

        return self.panels

    # ── Session management ─────────────────────────────────────────────────────

    def _get_session_dir(self) -> Path:
        if self._session_dir is None:
            from utils.session_store import get_session_dir
            self._session_dir = get_session_dir()
            self._session_dir.mkdir(parents=True, exist_ok=True)
        return self._session_dir

    def new_session(self) -> dict:
        """
        Create a blank session and wire it into agent memory.
        Auto-summarises the outgoing session (if >= 3 turns) so its key ideas
        can be injected as a preamble into the new session's early queries.
        Returns session metadata.
        """
        import uuid as _uuid
        from utils.session_store import TruncatedJSONLChatMessageHistory, session_jsonl_path, upsert_index

        # Clear in-session memory note — new session starts clean.
        self._memory_summary_note = None
        self._last_summarize_ts = 0.0

        # Reuse existing 0-turn session to prevent "New conversation" clutter in
        # the History dropdown (e.g. multiple model inits + New Chat clicks).
        if self._active_session_meta and self._active_session_meta.get("turn_count", 0) == 0:
            now = int(time.time() * 1000)
            self._active_session_meta["updatedAt"] = now
            sd = self._get_session_dir()
            upsert_index(sd, dict(self._active_session_meta))
            return dict(self._active_session_meta)

        self._prev_session_summary = None

        session_id = str(_uuid.uuid4())
        sd = self._get_session_dir()
        history = TruncatedJSONLChatMessageHistory(session_jsonl_path(sd, session_id))
        self.memory.chat_memory = history
        now = int(time.time() * 1000)
        meta: dict = {
            "id": session_id,
            "title": "New conversation",
            "createdAt": now,
            "updatedAt": now,
            "turn_count": 0,
        }
        self._active_session_id = session_id
        self._active_session_meta = meta
        upsert_index(sd, dict(meta))
        return meta

    def load_session(self, session_id: str, keep_last_n: int = 10) -> dict:
        """Load an existing session into agent memory. Returns session metadata + all turns."""
        # Loading an existing session is a continuation, not a fresh start —
        # clear any cross-session summary preamble so it doesn't bleed in.
        self._prev_session_summary = None
        self._memory_summary_note = None
        self._last_summarize_ts = 0.0

        import ast
        from utils.session_store import (
            TruncatedJSONLChatMessageHistory,
            session_jsonl_path,
            load_index,
            read_session_turns,
        )
        sd = self._get_session_dir()
        path = session_jsonl_path(sd, session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session {session_id} not found.")
        history = TruncatedJSONLChatMessageHistory(path, keep_last_n=keep_last_n)
        self.memory.chat_memory = history
        self._active_session_id = session_id
        idx = load_index(sd)
        meta = next((e for e in idx if e.get("id") == session_id), None)
        self._active_session_meta = meta
        raw_turns = read_session_turns(sd, session_id)

        # Clean up turns for UI display:
        #   • Strip the " | Working directory: ..." suffix that convchain_api appends to queries.
        #   • Parse stringified Python lists/dicts (raw agent output) before normalizing.
        cleaned_turns = []
        for t in raw_turns:
            query = t["query"]
            for marker in (" | Working directory:", " | No working directory provided"):
                if marker in query:
                    query = query[:query.index(marker)].rstrip()
                    break

            raw_resp = t["response"]
            try:
                parsed = ast.literal_eval(raw_resp)
                raw_resp = parsed
            except Exception:
                pass
            try:
                response = self._normalize_agent_output(raw_resp)
                response = self._extract_display_response(response)
            except Exception:
                response = t["response"]

            cleaned_turns.append({**t, "query": query, "response": response})

        return {"session": meta, "turns": cleaned_turns}

    def list_sessions(self) -> List[dict]:
        """Return sessions from index, sorted newest-first.

        Automatically garbage-collects index.json:
        - Removes entries whose JSONL file no longer exists (manual deletes).
        - Hides 0-turn sessions from the dropdown unless they are the active session
          (they have no content worth loading).
        """
        from utils.session_store import load_index, save_index, session_jsonl_path
        sd = self._get_session_dir()
        entries = load_index(sd)

        live: List[dict] = []
        orphans_removed = False
        for e in entries:
            sid = e.get("id", "")
            jsonl = session_jsonl_path(sd, sid)
            if not jsonl.exists():
                orphans_removed = True
                continue  # prune: JSONL was manually deleted
            if e.get("turn_count", 0) == 0 and sid != self._active_session_id:
                continue  # hide empty shells from dropdown
            summary = (e.get("summary") or "").strip()
            e["has_summary"] = bool(summary)
            e["summary_preview"] = (
                summary[:80].rstrip() + ("…" if len(summary) > 80 else "")
                if summary else ""
            )
            live.append(e)

        if orphans_removed:
            # Persist the cleaned index so orphans don't reappear.
            save_index(sd, [e for e in entries if session_jsonl_path(sd, e.get("id", "")).exists()])

        return sorted(live, key=lambda e: e.get("updatedAt", 0), reverse=True)

    def delete_session(self, session_id: str) -> None:
        """Delete a session's JSONL file and remove it from the index."""
        from utils.session_store import session_jsonl_path, remove_from_index
        sd = self._get_session_dir()
        path = session_jsonl_path(sd, session_id)
        if path.exists():
            path.unlink()
        remove_from_index(sd, session_id)
        if session_id == self._active_session_id:
            self._active_session_id = None
            self._active_session_meta = None

    def _build_cleaned_turns(self, session_id: str) -> list:
        """Read + clean turns for a session (strips dir hints, normalises agent output)."""
        import ast
        from utils.session_store import read_session_turns
        raw = read_session_turns(self._get_session_dir(), session_id)
        cleaned = []
        for t in raw:
            q = t["query"]
            for marker in (" | Working directory:", " | No working directory provided"):
                if marker in q:
                    q = q[: q.index(marker)].rstrip()
                    break
            raw_resp = t["response"]
            try:
                raw_resp = ast.literal_eval(raw_resp)
            except Exception:
                pass
            try:
                response = self._normalize_agent_output(raw_resp)
                response = self._extract_display_response(response)
            except Exception:
                response = t["response"]
            cleaned.append({**t, "query": q, "response": response})
        return cleaned

    def summarize_active_session(self) -> str:
        """
        Summarise the currently active session with the LLM, store the result in
        index.json, and return the summary string.  Returns "" if no active session,
        no model, or the session has fewer than 3 turns.
        """
        if not self._active_session_id or not self.llm_model:
            return ""
        if (self._active_session_meta or {}).get("turn_count", 0) < 3:
            return ""
        try:
            from utils.session_store import summarize_session_turns, upsert_index
            turns = self._build_cleaned_turns(self._active_session_id)
            summary = summarize_session_turns(turns, self.llm_model)
            if summary and self._active_session_meta is not None:
                self._active_session_meta["summary"] = summary
                self._active_session_meta["summarized_at"] = int(time.time() * 1000)
                upsert_index(self._get_session_dir(), dict(self._active_session_meta))
            return summary
        except Exception:
            return ""

    def summarize_session(self, session_id: str) -> dict:
        """
        Summarise any session by ID (used by the API endpoint).
        If the requested session is not the active one, reads its turns directly.
        Stores the summary in index.json and returns {"session_id", "summary"}.
        """
        if not self.llm_model:
            raise RuntimeError("Initialize a model before summarizing.")
        from utils.session_store import (
            summarize_session_turns, upsert_index, load_index, session_jsonl_path
        )
        sd = self._get_session_dir()
        if not session_jsonl_path(sd, session_id).exists():
            raise FileNotFoundError(f"Session '{session_id}' not found.")

        if session_id == self._active_session_id:
            summary = self.summarize_active_session()
        else:
            turns = self._build_cleaned_turns(session_id)
            summary = summarize_session_turns(turns, self.llm_model)
            if summary:
                idx = load_index(sd)
                meta = next((e for e in idx if e.get("id") == session_id), {})
                meta["summary"] = summary
                meta["summarized_at"] = int(time.time() * 1000)
                upsert_index(sd, meta)

        return {"session_id": session_id, "summary": summary}

    def _retrieve_session_context(self, query: str) -> str:
        """
        Returns a context block (hybrid fuzzy+cosine search over the current
        session's older turns) to prepend to the agent input each query.
        Cross-session retrieval is intentionally excluded — it is triggered
        only on explicit user request via the agent tool (Piece 2).
        """
        blocks: list = []

        # ── Hybrid search over current session's older turns ──────────────────
        # Cross-session context (previous session summary) is intentionally NOT
        # injected here automatically. A new chat is a clean slate. Retrieval from
        # past sessions is triggered only when the user explicitly asks (agent tool).
        if self._active_session_id:
            try:
                from utils.session_store import session_history_search, session_jsonl_path
                path = session_jsonl_path(self._get_session_dir(), self._active_session_id)
                retrieved = session_history_search(
                    session_jsonl_path=path,
                    query=query,
                    keep_last_n=10,
                    top_k=4,
                    threshold=0.25,
                )
                if retrieved:
                    lines = ["[RETRIEVED CONTEXT — relevant prior conversation turns]"]
                    for t in retrieved:
                        q = t["query"]
                        for marker in (" | Working directory:", " | No working directory provided"):
                            if marker in q:
                                q = q[: q.index(marker)].rstrip()
                                break
                        resp = t["response"]
                        if len(resp) > 600:
                            resp = resp[:597] + "…"
                        lines.append(f"Turn {t['turn_index'] + 1}:")
                        lines.append(f"  User: {q[:400]}")
                        lines.append(f"  Assistant: {resp}")
                        lines.append("---")
                    lines.append("[END RETRIEVED CONTEXT]")
                    blocks.append("\n".join(lines))
            except Exception:
                pass

        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n"

    def _update_session_index(self, first_query: str = "") -> None:
        """Update index.json after a turn completes (title, timestamp, turn count)."""
        if not self._active_session_id or not self._active_session_meta:
            return
        try:
            from utils.session_store import upsert_index
            meta = self._active_session_meta
            meta["updatedAt"] = int(time.time() * 1000)
            meta["turn_count"] = meta.get("turn_count", 0) + 1
            if meta.get("title") == "New conversation" and first_query:
                q = first_query.strip()
                meta["title"] = (q[:43] + "…") if len(q) > 46 else q
            self._active_session_meta = meta
            upsert_index(self._get_session_dir(), dict(meta))
        except Exception:
            pass

    #-----------------------------------------------------------------------------------------------------------------------------------------
    # API‑friendly wrapper: returns plain data instead of Dash components
    #-----------------------------------------------------------------------------------------------------------------------------------------
    def convchain_api(self, query: str) -> Dict[str, Any]:
        """
        Run a query through the agent and return a JSON-serializable
        payload suitable for REST/streaming APIs (no Dash components).
        """
        if not query:
            return {"error": "Empty query."}

        if not self.llm_model or not self.embedding_model or not self.ai_assistant_agent:
            return {"error": "No model initialized. Please initialize an LLM before querying."}

        # Build one single “input” string that includes the directory hint
        if self.selected_directory:
            global_directory_message = f" | Working directory: {self.selected_directory}"
        else:
            global_directory_message = f" | No working directory provided"
        query_with_directory = query.strip() + global_directory_message

        tool_query = query_with_directory

        # Prepend relevant prior turns retrieved via hybrid fuzzy+cosine session search.
        _session_ctx = self._retrieve_session_context(query)
        if _session_ctx:
            tool_query = _session_ctx + tool_query

        # Prepend in-session compressed memory note if the user clicked "Summarize memory".
        if self._memory_summary_note:
            tool_query = (
                f"[COMPRESSED SESSION MEMORY]\n{self._memory_summary_note}\n"
                f"[END COMPRESSED MEMORY]\n\n" + tool_query
            )

        # reset reasoning log for this query
        self.reset_reasoning_events()
        # Seed the reasoning log so UIs see something immediately
        self.reasoning_events.append(f"[QUERY] {query_with_directory}")
        if _session_ctx:
            n = _session_ctx.count("Turn ")
            if n:
                self.reasoning_events.append(f"[MEMORY] Injected {n} retrieved turn(s) via hybrid fuzzy+cosine search.")
        if self._memory_summary_note:
            self.reasoning_events.append("[MEMORY] Prepended compressed session memory note.")
        skill_context = self.build_skill_context(query)
        _sr = self._last_skill_result
        if _sr and (_sr.loaded_bundled_count + _sr.loaded_user_count) > 0:
            for label in _sr.loaded_labels:
                self.reasoning_events.append(label)
        elif skill_context:
            self.reasoning_events.append("[SYSTEM] Global skill policy injected.")
        for msg in (self.skill_warnings[:5] if not _sr else [w for w in self.skill_warnings[:5] if "blocked" in w.lower() or "warning" in w.lower()]):
            self.reasoning_events.append(f"[SKILL WARNING] {msg}")

        # Log reasoning before calling the agent
        self.logger.info("\n============================NEW QUERY============================")
        self.logger.debug(f"{tool_query}")

        try:
            result = self._invoke_agent_and_recover(
                {
                    "input": tool_query,
                    "global_directory_message": global_directory_message,
                    "skill_context": skill_context,
                    "runtime_context": self.runtime_context,
                }
            )
        except openai.BadRequestError as e:
            # Graceful recovery for context overflow: clear memory and retry once.
            err_text = str(e)
            context_overflow = (
                "context_length_exceeded" in err_text
                or "Input tokens exceed" in err_text
                or "maximum context length" in err_text
                or "input_tokens" in err_text
            )
            if context_overflow:
                # Progressive memory compression — NEVER clears JSONL.
                # Step 1: summarize into a memory note and shrink the agent window.
                self.reasoning_events.append(
                    "[SYSTEM] Context limit reached. Compressing memory and retrying…"
                )
                try:
                    self.summarize_chat_history(keep_last_turns=2)
                except Exception:
                    pass

                # Build a compact retry query with the compressed note.
                _retry_base = query_with_directory
                if self._memory_summary_note:
                    _retry_base = (
                        f"[COMPRESSED SESSION MEMORY]\n{self._memory_summary_note}\n"
                        f"[END COMPRESSED MEMORY]\n\n" + _retry_base
                    )

                # Progressive keep_last reduction: 2 → 1.
                result = None
                for _keep_n in (2, 1):
                    if hasattr(self.memory.chat_memory, "_keep"):
                        self.memory.chat_memory._keep = _keep_n
                    self.reasoning_cb.step_count = 0
                    try:
                        result = self._invoke_agent_and_recover(
                            {
                                "input": _retry_base,
                                "global_directory_message": global_directory_message,
                                "skill_context": skill_context,
                                "runtime_context": self.runtime_context,
                            }
                        )
                        self.reasoning_events.append(
                            f"[SYSTEM] Retry succeeded with keep_last_n={_keep_n}."
                        )
                        break
                    except Exception:
                        continue

                if result is None:
                    grace_hint = ""
                    if self.model_choice == GRACE_MODEL_ID:
                        grace_hint = (
                            " For Grace: disable extra skills or raise --max-model-len in "
                            "run_gemma4_31b_vllm.sbatch (e.g. 8192) and resubmit the Slurm job."
                        )
                    return {
                        "error": (
                            "Query exceeded model context limits even after memory compression. "
                            "Try starting a new chat or shortening your query."
                            f"{grace_hint}"
                        )
                    }
            else:
                err_text = str(e)
                if (
                    self.model_choice == GRACE_MODEL_ID
                    and "enable-auto-tool-choice" in err_text
                    and "tool-call-parser" in err_text
                ):
                    return {
                        "error": (
                            f"Model request failed: {e}\n\n"
                            f"{GRACE_VLLM_NATIVE_TOOLS_HINT}"
                        )
                    }
                return {"error": f"Model request failed: {e}"}
        except Exception as e:
            return {"error": f"Agent execution failed: {e}"}

        self.update_context_usage(tool_query, skill_context)
        raw_output = result.get("output", "")
        response = self._normalize_agent_output(raw_output)
        response = sanitize_user_markdown(response)
        display_response = self._extract_display_response(response)
        display_response = sanitize_user_markdown(display_response)

        # If user explicitly asked for raw/copyable LaTeX source, preserve code blocks as-is.
        query_lower = query.lower()
        wants_raw_latex = any(
            phrase in query_lower
            for phrase in [
                "latex code",
                "latex source",
                "raw latex",
                "copyable latex",
                "tex code",
                "source code in latex",
            ]
        )

        if wants_raw_latex:
            display_response_md = display_response
        else:
            # LLM-based markdown/math formatting pass.
            display_response_md = LLMMarkdownParser(self.llm_model, display_response)
            display_response_md = MarkdownParser(display_response_md)
        display_response_md = sanitize_user_markdown(display_response_md)
        if not display_response_md:
            display_response_md = display_response

        intermediate_steps = result.get("intermediate_steps", [])

        self._update_session_index(query)

        return {
            "query": query,
            "response": display_response_md,
            "raw_response": response,
            "intermediate_steps": [str(step) for step in intermediate_steps],
            "reasoning_events": list(self.reasoning_events),
            "working_directory": self.selected_directory,
            "active_skill_ids": [skill["id"] for skill in self.last_loaded_skills],
            "skill_warnings": list(self.skill_warnings),
        }

# Instantiate the global chatbot
cb = Chatbot()

# ---------------------------------------------------------------------#
# B.  Legacy Dash UI (deprecated)                                      #
# ---------------------------------------------------------------------#
def layout() -> html.Div:
    _raise_dash_deprecation()
    return html.Div(
        [
            html.Script(src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js", **{"async": True}),
            # logo
            html.Img(
                src="/assets/Logo2.JPG",
                className="w-20 h-auto mb-8 drop-shadow-lg",
                alt="DecodedAI logo",
            ),
            html.H1("RADIANT-LLM",
                    className="text-4xl font-extrabold text-center mb-4",
                    # style={"textAlign": "center", "fontFamily": "Lucida Calligraphy, cursive"}),
                    style={"textAlign": "center"}),

            html.H2("Your Personal AI Assistant for a Safe, Secure, and Safeguarded Nuclear Future",
                    className="text-4xl font-extrabold text-center mb-4",
                    style={"textAlign": "center", "fontFamily": "Italics"}),
                    

            html.Div([
                # Left Sidebar: Settings and External Alerts
                html.Div([
                    html.H3("Settings", className="font-bold text-xl mb-2"),
                    html.Label("Select a Model (*)"),
                    dcc.Dropdown(
                        id="model-selector",
                        options=[
                            {"label": "gpt-5.5-augmented", "value": "gpt-5.5"},
                            {"label": "gpt-5.4-augmented", "value": "gpt-5.4"},
                            {"label": "gpt-5.3-chat-latest-augmented", "value": "gpt-5.3-chat-latest"},
                            {"label": "gpt-5.2-augmented(EXPENSIVE!)", "value": "gpt-5.2"},
                            {"label": "gpt-5.1-augmented", "value": "gpt-5.1"},
                            {"label": "gpt-5-augmented", "value": "gpt-5"},
                            {"label": "gpt-4.1-augmented", "value": "gpt-4.1"},
                            {"label": "GPT-4o-augmented", "value": "gpt-4o"},
                            # {"label": "Omni-1 (o1-augmented:EXPENSIVE!)", "value": "o1"},
                            # {"label": "Omni-3 (o3-mini-augmented)", "value": "o3-mini"},
                            # {"label": "Omni-3 (o3-augmented)", "value": "o3"},
                            {"label": "Gemini-3.1-pro-preview: augmented", "value": "gemini-3.1-pro-preview"},
                            {"label": "Gemini-3-pro-preview: augmented", "value": "gemini-3-pro-preview"},
                            {"label": "Gemini 2.5-flash: augmented", "value": "gemini-2.5-flash"},
                            {
                                "label": "Grace Gemma 4 31B (local vLLM)",
                                "value": GRACE_MODEL_ID,
                            },
                            ],
                        placeholder="Select a model",
                        className="p-5 bg-gray-100 rounded-xl shadow",
                    ),
                    html.Br(),

                    dbc.Button("Initialize", id="initialize-button", color="primary", n_clicks=0, size="sm", style={"width": "100px"}),
                    html.Br(), 
                    html.Br(),

                    #------------------- Place holder for Initilized llm models---------------
                    html.Div(id="init-alert-radiant_llm"),
                    html.Br(),  
                    #------------------- Place holder for Working Directory-------------------

                    html.Label("Working Directory (Optional)"),
                    dcc.Input(
                        id="directory-selector-radiant_llm",
                        type="text",
                        placeholder="Enter directory path...",
                        style={"width": "100%"},
                        className="p-5 bg-gray-100 rounded-xl shadow"
                    ),
                    html.Div(id="directory-alert-radiant_llm",
                    style={"whiteSpace": "normal",
                            "wordBreak": "break-word",
                            "overflowWrap": "break-word",
                            "maxWidth": "400px"
                            }), 
                    html.Br(),

                    html.Label("Get to know some NSE basics 😊"),
                    dcc.Dropdown(
                        id="query-selector",
                        options=[{"label": q, "value": q} for q in basic_queries],
                        placeholder="Select a query(optional)"
                    ),
                    html.Br(),

                    # Container for external alerts (warnings from your core functions)
                    html.H4("Alerts"),
                    # Button to clear the alerts
                    dbc.Button("Clear Alerts", id="clear-alerts-button", color="danger", size="sm", style={"width": "100%", "marginBottom": "10px"}),
                    html.Div(id="external-alerts-radiant_llm")
                ], style={"width": "25%", "display": "inline-block", "verticalAlign": "top", "padding": "10px"}),

                # Main Content: Conversation
                html.Div([
                    html.H3("Conversation", className="font-bold text-xl mb-2"),
                    dcc.Textarea(
                        id="query-input-radiant_llm",
                        placeholder="Type your query here...",
                        # ← keep only the utilities you like
                        className=(
                            "w-full h-28 p-4 "
                            "border border-gray-300 rounded-lg "           #  thin grey outline
                            "bg-white "                                     #  no grey panel; delete if you prefer
                            "focus:ring-2 focus:ring-blue-500 resize-y"  #  keep nice focus ring
                        ),
                        style={"width": "100%", "height": "100px"}
                    ),
                    html.Br(),
                    dbc.Button("Submit", id="submit-button", color="primary", n_clicks=0, size="sm", style={"width": "100px"}),
                    html.Br(), html.Br(),
                    html.Div(id="chat-output-radiant_llm",
                        style={"border": "1px solid #ccc", "padding": "10px", "height": "1000px", "overflowY": "scroll"}
                    )
                ], style={"width": "70%", "display": "inline-block", "verticalAlign": "top", "padding": "10px"})
            ]),

            # dcc.Store to hold external alerts data
            dcc.Store(id="external-alerts-radiant_llm-store", data=[]),
            # Interval to poll for external alerts
            dcc.Interval(id="alerts-interval", interval=2000, n_intervals=0),
            # Invisible div for clientside MathJax callback
            html.Div(id="dummy-radiant_llm", style={"display": "none"})
        ]
    )

# ---------------------------------------------------------------------#
# B.  Wire callbacks into *whatever* Dash app is passed in             #
# ---------------------------------------------------------------------#         

# --------- CALLBACK REGISTRATION ------------------------------------
def register_callbacks(app: dash.Dash):
    _raise_dash_deprecation()
    # 1) initialize model
    @app.callback(
        Output("init-alert-radiant_llm", "children"),
        Input("initialize-button", "n_clicks"),
        State("model-selector", "value"),
    )
    def on_initialize(n, model):
        if n and model:
            alerts = cb.initialize_models(model)
            return html.Div(alerts)
        return ""

    # 2) set working directory
    @app.callback(
        Output("directory-alert-radiant_llm", "children"),
        Input("directory-selector-radiant_llm", "value"),
    )
    def on_set_dir(val):
        return cb.set_directory(val.strip()) if val else ""

    # 3) Submit query callback (multi-output: updates chat and clears query input)
    @app.callback(
        Output("chat-output-radiant_llm", "children"),
        Output("query-input-radiant_llm", "value"),
        Input("submit-button", "n_clicks"),
        State("query-input-radiant_llm", "value"),
        State("query-selector", "value"),
        State("chat-output-radiant_llm", "children")
    )
    def handle_query(n_clicks, input_query, selected_query, current_chat):
        if n_clicks:
            if selected_query and input_query:
                new_message = dbc.Alert("Please submit only one query: either select from the dropdown or type one manually.", color="warning")
                updated_chat = (current_chat or []) + [new_message]
                return updated_chat, ""
            else:
                query = selected_query if selected_query else input_query
                if query:
                    panels = cb.convchain(query)
                    new_message = panels[-1]
                else:
                    new_message = ""
                updated_chat = (current_chat or []) + [new_message]
                # Clear the query input area
                return updated_chat, ""
        return current_chat, (input_query or "")

    # 4) Clientside callback to re-run MathJax after chat updates
    app.clientside_callback(
        """
        function(children) {
        if (window.MathJax) {
            window.MathJax.typesetPromise();
        }
        return "";
        }
        """,
        Output("dummy-radiant_llm", "children"),
        Input("chat-output-radiant_llm", "children")
    )

    # 5) **Single** callback for both clearing and displaying alerts
    @app.callback(
    Output("external-alerts-radiant_llm", "children"),
    Input("alerts-interval", "n_intervals"),
    Input("clear-alerts-button", "n_clicks"),
    prevent_initial_call=True,
    )
    def manage_alerts(n_intervals, clear_clicks):
        ctx = dash.callback_context
        # if the clear button fired, empty the list
        if ctx.triggered_id == "clear-alerts-button":
            global_external_alerts.clear()
        # in either case, render whatever’s in global_external_alerts
        return [dbc.Alert(msg, color="warning") for msg in global_external_alerts]

# # ---------------------------------------------------------------------
# #  C.  Optional standalone runner  (only runs if you execute the file)  
# # ---------------------------------------------------------------------
# if __name__ == "__main__":
#     from dash import Dash
#     from utils.general_utilities import free_port_finder
#     import webbrowser, os                       # already imported earlier? fine

#     # 1  Tailwind + Bootstrap stylesheets
#     external_stylesheets = [
#         "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css",
#         "https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css",
#     ]

#     # 2  Build a *local* Dash app only for standalone mode
#     dash_app = Dash(
#         __name__,
#         external_stylesheets=external_stylesheets,
#         suppress_callback_exceptions=True,
#     )
#     dash_app.title = "radiant_llm"                # or AutoGEANT4, AutoPHITS

#     # 3  Plug in layout + callbacks that you already defined
#     dash_app.layout = layout()
#     register_callbacks(dash_app)

#     # 4  Run
#     port = free_port_finder()
#     if os.environ.get("IN_DOCKER") != "1":
#         webbrowser.open_new(f"http://127.0.0.1:{port}")
#     dash_app.run_server(host="0.0.0.0", port=port, debug=True)


# =========================================================================
# Legacy standalone entrypoint (deprecated — use api.py / radiant-llm-api)
# =========================================================================
if __name__ == "__main__":
    _raise_dash_deprecation()
