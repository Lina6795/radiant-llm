# A Vitual Assitant for SAM Users: Based on LLM Augmentation and AI Agents. 
import os
import matplotlib.pyplot as plt
from difflib import get_close_matches
import base64
import openai
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
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langchain.agents import create_tool_calling_agent, create_openai_tools_agent, AgentExecutor
from langchain_openai import ChatOpenAI

# Memory capabilities 
from langchain.prompts import MessagesPlaceholder
from langchain.memory import ConversationBufferMemory
from langchain.agents import initialize_agent
from langchain_community.agent_toolkits.load_tools import load_tools

from typing import  Annotated, Sequence, Callable
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

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
from typing import Optional, List, Dict, Any
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

# WorkSataion Execution
import paramiko

# Custom Utilities 
# *************** More for ERROR RESOLUTION PIPELINE ***************
import difflib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from .alerts import global_external_alerts

#
warnings.filterwarnings("ignore", category=UserWarning, module='pydantic')
# -------------------------------
# Global variable for external alerts
# -------------------------------
# global_external_alerts = []  # This list will be updated by your core functions outside callbacks
# global cb # This is the Chatbot class defined as a global variable 



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


###################################################################################
############################# Utility functions Start Here ########################
###################################################################################
# =====================================================================
#  DOS to UNIX conversion Logic
# =====================================================================
# from your_dash_app import app
import socket
def free_port_finder():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # bind to port 0 → OS picks an available port
    s.bind(("", 0))
    _, port = s.getsockname()
    s.close()
    return port
#=============================================================================
# Prompt Finetuning  Logic (with ReAct format)
#=============================================================================
from langchain_core.tools import BaseTool
def PromptFinetuning(query: str, 
                     tool_names: Optional[Sequence[str]] = None) -> str:
    """
    Wraps the user's query with system instructions:
      1) Preserve any http:// or https:// URL exactly, verbatim.
      2) Only ask for a working directory if the query explicitly
         involves file- or directory-based operations.
      3) Guide the agent to use Thought/Action/Observation reasoning.
    """
    # 1) Echo the user’s query
    wrapped = f"Prompt: {query}\n\n"

    # 2) URL‐preservation instruction
    wrapped += (
        "FURTHER INSTRUCTIONS:\n"
        "1) Whenever you see a URL in the user's input (anything matching http:// or https://),\n"
        "   copy it exactly—verbatim—into any downstream tool call or query.\n"
        "   Do not paraphrase, truncate, or otherwise alter the URL in any way.\n\n"
    )

    # 3) Directory‐prompt suppression
    wrapped += (
        "2) Only ask the user to provide a working directory IF AND ONLY IF the\n"
        "   query clearly requires file- or directory-based operations, e.g.: reading,\n"
        "   summarizing, listing, opening, writing, executing, plotting, or saving files\n"
        "   from a local folder. For all other queries (greetings, conceptual questions,\n"
        "   math problems, definitions, etc.), do NOT mention or ask about any directory.\n\n"
    )

    # 4) ReAct Thought/Action/Observation format
    wrapped += (
        "3) When you answer, follow this schema exactly (but DO NOT show it in your final output; it’s for your internal reasoning ONLY):\n\n"
        "Question: the input question\n"
        "Thought: reflect on what to do next\n"
        "Action: the name of the tool to call, one of [{tool_names}]\n"
        "Action Input: the input to that tool\n"
        "Observation: the result returned by the tool\n"
        "...(Thought/Action/Action Input/Observation can repeat N times as needed)...\n"
        "Thought: I now know the final answer\n"
        "Final Answer: the answer to the original question\n"
    
        "Begin!\n\n"
        f"Question: {query}\n"
        "Thought:"
    )
    return wrapped
#=============================================================================

def url_preservation_for_websearch(query: str) -> str:
    """
    Echoes the original query and appends a system instruction
    to preserve any URLs verbatim in downstream tool invocations.
    """
    # 1) Echo the incoming query
    # print(f"🔎 Query received: {query}")

    # 2) System instruction to preserve URLs exactly
    instruction = (
        "FURTHER INSTRUCTIONS:\n"
        "Whenever you see a URL in the user's input (anything matching http:// or https://),\n"
        "you must copy that URL exactly—verbatim—into any downstream tool invocation or query.\n"
        "Do not paraphrase, truncate, replace, or otherwise alter the URL in any way."
    )

    # 3) Return both the echoed query and the preservation instruction
    return f"QUERY: {query}\n\n{instruction}"

# =============================================================================
# Usage
# query  = f"what is the temperature correlation for lead density as shown in this url\n"
# "[https://mooseframework.inl.gov/source/fluidproperties/LeadFluidProperties.html]?\n"
# "Do not change this url at all. Use this exact one"
# print(url_preservation_for_websearch(query))
# =============================================================================


# =====================================================================
#  DOS to UNIX conversion Logic
# =====================================================================

def convert_to_unix(file_path: Path):
    """
    Converts CRLF to LF using a safer method than fileinput for Windows.
    Keeps a .bak backup and writes with Unix line endings.
    """
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    file_path.replace(backup)

    with open(backup, "r", encoding="utf-8") as src, open(file_path, "w", encoding="utf-8", newline='\n') as dst:
        for line in src:
            dst.write(line.rstrip('\r\n') + '\n')

# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

# =====================================================================
#                       Global Alerts for the Chat bot 
# =====================================================================
def GeneralAlerts(alert_sink, message, color="info", dismissable=True):
    """
    Appends a dbc.Alert to alert_sink.
    - alert_sink: a list you’re collecting alerts into
    - message:   the text to show
    - color:     bootstrap alert color (info, success, warning, danger…)
    - dismissable: whether the alert shows a close button (defaults to True)
    """
    if alert_sink is not None:
        alert_sink.append(
            dbc.Alert(message,
                      color=color,
                      dismissable=dismissable,
                      style={"whiteSpace": "normal",
                              "wordBreak": "break-word",
                              "overflowWrap": "break-word",
                              "maxWidth": "300px",
                            },
                    )
        )
# =====================================================================
#                       CONVERT OUTPUT TO MARKDOWN
# =====================================================================
logger = logging.getLogger(__name__)

def MarkdownParser(md: str) -> str:
    """
    Reformats LaTeX delimiters in a Markdown string so the React UI (remark-math
    + rehype-katex) can render them. The React stack only recognizes $...$
    and $$...$$; it does NOT parse \\(...\\) or \\[...\\].

    - \\( … \\) becomes $…$
    - \\[ … \\] becomes $$…$$
    - Naked block start: a line starting with \\begin{aligned} (or \\begin{align})
      that is not already preceded by $$ gets an opening $$ inserted, so
      "…\\n  \\begin{aligned}…\\end{aligned}$$" still renders.
    - Preserves all other content.
    """
    # first, block-level \[ ... \]
    try:
        md = re.sub(
            r"\\\[\s*(.*?)\s*\\\]",
            lambda m: f"$${m.group(1).strip()}$$",
            md,
            flags=re.DOTALL,
        )
    except re.error as e:
        logger.warning(f"Block-math regex failed: {e!r}")

    # then, inline \( ... \)
    try:
        md = re.sub(
            r"\\\(\s*(.*?)\s*\\\)",
            lambda m: f"${m.group(1).strip()}$",
            md,
            flags=re.DOTALL,
        )
    except re.error as e:
        logger.warning(f"Inline-math regex failed: {e!r}")

    # fix naked block math lines without opening $$.
    # This catches lines like:
    #   \frac{...} ... }$$
    #   {\frac{...} ... }$$
    #   \begin{aligned} ... \end{aligned}$$
    # by inserting "$$" on the line above when needed.
    try:
        block_start = re.compile(
            r"^\{?\s*\\(?:begin\{(?:aligned|align)\}|frac|boxed|int|sum|prod|partial|nabla|mathbf\w+)"
        )
        lines = md.splitlines()
        fixed_lines = []
        for line in lines:
            stripped = line.strip()
            prev = fixed_lines[-1].strip() if fixed_lines else ""
            already_opened = stripped.startswith("$$")
            prev_has_block_open = prev.endswith("$$") or prev == "$$"
            if block_start.match(stripped) and not already_opened and not prev_has_block_open:
                fixed_lines.append("$$")
            fixed_lines.append(line)
        md = "\n".join(fixed_lines)
    except re.error as e:
        logger.warning(f"Naked-block regex failed: {e!r}")

    # repair common malformed display math where a closing $$ exists but the opening $$ is missing.
    # This is conservative: only patch if no opening $$ appears before the first $$.
    try:
        first_dollar_block = md.find("$$")
        if first_dollar_block != -1 and "$$" not in md[:first_dollar_block]:
            display_start = re.search(
                r"(\\begin\{(?:aligned|align)\}|\\frac|\\int|\\sum|\\prod|\\nabla|\\partial|\\mathbf\w*)",
                md[:first_dollar_block],
            )
            if display_start:
                s = display_start.start()
                md = f"{md[:s]}$$\n{md[s:]}"
    except re.error as e:
        logger.warning(f"Missing-opening-block regex failed: {e!r}")

    # if block delimiters are still odd, close at end as a last-resort fail-safe.
    if md.count("$$") % 2 == 1:
        md = f"{md}\n$$"

    return md

# =====================================================================
#                      LLM-based Markdown Parser
# =====================================================================

# Regex for the old formatter human prompt—if echoed, strip it (user must not see this).
_LLM_REPAIR_LEAK_PATTERN = re.compile(
    r"^\s*repair\s+this\s+markdown.*remark-math.*katex",
    re.IGNORECASE | re.DOTALL,
)
_FENCE_MARKDOWN_PATTERN = re.compile(r"^\s*```(?:markdown|md)?\s*$", re.IGNORECASE)
_LEADING_LIST_DECORATION_PATTERN = re.compile(r"^[>\-\*\d\.\)\s]+")
_TRAILING_REASONING_LOG_PATTERN = re.compile(r"\n+\s*reasoning\s+log\s*:?\s*$", re.IGNORECASE)


def _strip_llm_repair_preamble(text: str) -> str:
    """
    Remove leading lines that echo the internal formatter instruction so they
    never leak into the chat UI. Only strips when the opening looks like the
    formatter prompt, not arbitrary mentions of math renderers in body text.
    """
    if not text or not text.strip():
        return text
    s = text.strip()
    # Whole response is just the leak (or leak + whitespace)
    if _LLM_REPAIR_LEAK_PATTERN.match(s) and len(s) < 300:
        # If there's nothing after the instruction, fall back later to original md
        return ""

    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return ""

    opened_md_fence = False
    if _FENCE_MARKDOWN_PATTERN.match(lines[idx].strip()):
        opened_md_fence = True
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            return ""

    probe = lines[idx].strip()
    probe = _LEADING_LIST_DECORATION_PATTERN.sub("", probe).strip()
    probe_l = probe.lower()

    if probe_l.startswith("repair this markdown"):
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if opened_md_fence and idx < len(lines) and lines[idx].strip() == "```":
            idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
        return "\n".join(lines[idx:]).strip() if idx < len(lines) else ""

    # Handle cases where the leak appears in first few lines with wrappers/quotes.
    head_block = "\n".join(lines[idx : min(idx + 4, len(lines))])
    if _LLM_REPAIR_LEAK_PATTERN.search(head_block):
        j = idx
        while j < len(lines) and lines[j].strip():
            j += 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return "\n".join(lines[j:]).strip() if j < len(lines) else ""

    return text.strip()


def sanitize_user_markdown(text: str) -> str:
    """
    Final user-facing sanitation for markdown text.
    Removes leaked internal formatter instructions and a trailing
    standalone "Reasoning log" line if present.
    """
    if not text:
        return text
    cleaned = _strip_llm_repair_preamble(text)
    if not cleaned:
        return cleaned
    cleaned = _TRAILING_REASONING_LOG_PATTERN.sub("", cleaned).strip()
    return cleaned


def _normalize_llm_markdown_output(output) -> str:
    """
    Normalize LLM formatter outputs that may arrive as strings, LangChain
    message objects, or Responses-style content blocks.
    """
    if output is None:
        return ""

    if isinstance(output, str):
        return output

    if hasattr(output, "content") and not isinstance(output, (dict, list)):
        try:
            content = getattr(output, "content")
            if content is not None:
                return _normalize_llm_markdown_output(content)
        except Exception:
            pass

    if isinstance(output, dict):
        txt = output.get("text")
        if txt is not None:
            return _normalize_llm_markdown_output(txt)
        content = output.get("content")
        if content is not None:
            return _normalize_llm_markdown_output(content)
        return json.dumps(output, ensure_ascii=False)

    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and item.get("text") is not None:
                    normalized = _normalize_llm_markdown_output(item.get("text"))
                    if normalized.strip():
                        parts.append(normalized)
                    continue
                txt = item.get("text")
                if txt is not None:
                    normalized = _normalize_llm_markdown_output(txt)
                    if normalized.strip():
                        parts.append(normalized)
                    continue
                content = item.get("content")
                if content is not None:
                    normalized = _normalize_llm_markdown_output(content)
                    if normalized.strip():
                        parts.append(normalized)
                    continue
            elif isinstance(item, str) and item.strip():
                parts.append(item)
            else:
                normalized = _normalize_llm_markdown_output(item)
                if normalized.strip():
                    parts.append(normalized)
        merged = "\n".join(p for p in parts if p).strip()
        if merged:
            return merged
        return json.dumps(output, ensure_ascii=False)

    return str(output)


def LLMMarkdownParser(llm_model, md: str) -> str:
    """
    LLM-based markdown/math formatter.
    This pass focuses on math-delimiter/rendering fixes and is intended
    to preserve wording as much as possible.
    """
    if not md or llm_model is None:
        return md

    try:
        repair_messages = [
            SystemMessage(
                content=(
                    "You are a strict Markdown+LaTeX formatter. "
                    "Fix ONLY math-delimiter/rendering issues while preserving meaning and wording. "
                    "Rules: use $...$ inline, $$...$$ block; never use \\(\\) or \\[\\]; "
                    "balance all $$ delimiters; remove stray leading/trailing braces around equations; "
                    "do not rewrite, summarize, reorder, or add any prose; "
                    "outside math delimiters, keep text identical character-for-character whenever possible; "
                    "preserve existing fenced code blocks exactly (including ```latex blocks). "
                    "CRITICAL: Output ONLY the repaired Markdown document. "
                    "Do not repeat, quote, or summarize these instructions. "
                    "Do not output any preamble like 'Repair this Markdown' or mention remark-math/KaTeX."
                )
            ),
            HumanMessage(
                content=(
                    "Apply delimiter rules to the document below. "
                    "Return the full document only—no title, no explanation, no preamble.\n\n"
                    "---BEGIN---\n"
                    f"{md}\n"
                    "---END---"
                )
            ),
        ]
        repaired = llm_model.invoke(repair_messages)
        repaired_text = _normalize_llm_markdown_output(repaired)
        repaired_text = repaired_text.strip()
        # Strip ---BEGIN---/---END--- wrappers if the model echoed them
        if repaired_text.startswith("---BEGIN---"):
            repaired_text = repaired_text[len("---BEGIN---") :].lstrip()
        if repaired_text.endswith("---END---"):
            repaired_text = repaired_text[: -len("---END---")].rstrip()
        # Last line of defense: remove leaked formatter instructions
        repaired_text = sanitize_user_markdown(repaired_text)

        return repaired_text if repaired_text else md
    except Exception:
        return md

# =====================================================================
#                       OSTI.GOV ID UTILITY
# =====================================================================

from urllib.parse import urlparse, unquote
from cgi import parse_header

def get_osti_pdf_link(osti_id: str) -> str | None:
    """
    Query the OSTI API for the record JSON. 
    First try to find a direct .pdf URL; 
    otherwise fall back to the 'fulltext' link.
    Returns None if any network or API error occurs.
    """
    try:
        api_url = f"https://www.osti.gov/api/v1/records/{osti_id}"
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[{osti_id}] API request failed: {e}")
        return None

    records = data if isinstance(data, list) else [data]

    for rec in records:
        links = rec.get("links", [])
        for link in links:
            href = link.get("href", "")
            if href.lower().endswith(".pdf"):
                return href
        for link in links:
            if link.get("rel") == "fulltext":
                return link.get("href")
    return None
#=======================================================================

def file_downloader(
    url_input: str | list[str],
    working_directory: str
) -> str:
    """
    Downloads files (images, PDFs, etc.) from the provided URLs and saves them to
    the working directory. Automatically fixes OSTI links to point at the real PDF.
    """
    #-------------------------------------------
    # Helper Utility
    #-------------------------------------------
    def get_osti_pdf_link(osti_id: str) -> str | None:
        """
        Query the OSTI API for the record JSON. 
        First try to find a direct .pdf URL; 
        otherwise fall back to the 'fulltext' link.
        Returns None if any network or API error occurs.
        """
        try:
            api_url = f"https://www.osti.gov/api/v1/records/{osti_id}"
            resp = requests.get(api_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"[{osti_id}] API request failed: {e}")
            return None

        records = data if isinstance(data, list) else [data]

        for rec in records:
            links = rec.get("links", [])
            for link in links:
                href = link.get("href", "")
                if href.lower().endswith(".pdf"):
                    return href
            for link in links:
                if link.get("rel") == "fulltext":
                    return link.get("href")
        return None
    #-------------------------------------------
    # Main file_downloader Logic
    #-------------------------------------------
    Path(working_directory).mkdir(parents=True, exist_ok=True)

    # unify to a list
    urls = url_input if isinstance(url_input, list) else [u.strip() for u in url_input.split(",")]

    results = []
    for url in urls:
        original_url = url
        # 1) If it's an OSTI link, try to extract an OSTI ID
        if "osti.gov" in url:
            m = re.search(r"/(\d+)(?:$|[^0-9])", url)
            if m:
                osti_id = m.group(1)
                # 2) Ask the OSTI API for the real PDF link
                pdf_link = get_osti_pdf_link(osti_id)
                if pdf_link:
                    url = pdf_link

        try:
            # 3) Fetch the URL (following redirects)
            resp = requests.get(url, stream=True, timeout=20)
            resp.raise_for_status()

            # 4) Determine filename:
            #   a) from Content-Disposition header if present
            cd = resp.headers.get("Content-Disposition")
            if cd:
                _, params = parse_header(cd)
                fname = params.get("filename")
            else:
                # b) fallback: use last path segment
                path = urlparse(url).path
                fname = os.path.basename(path)

            # 5) Ensure .pdf extension if PDF content-type
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" in content_type and not fname.lower().endswith(".pdf"):
                fname += ".pdf"

            # 6) Save
            file_path = os.path.join(working_directory, unquote(fname))
            with open(file_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)

            # results.append(f"Downloaded: {file_path}")
            results.append(("Downloaded", os.path.basename(file_path), url))

        except Exception as e:
            results.append(f"Failed to download {original_url} → {url}: {e}")

    # return "\n".join(results)
    return results[0] if results else "No valid URLs provided"

#=====================================================================
# Utilities for Image Descriptions
#=====================================================================
import base64, io, json
from typing import List, Dict, Any
from PIL import Image

# utils/general_utilities.py

def call_vision_llm(
    images: List[bytes],
    prompt: str,
    cb,
    detail: str = "auto"
) -> str:
    """
    Given a list of PNG‐bytes and a text prompt, call the *right* vision LLM 
    based on cb.llm_type (“gemini” or “gpt”) and return its raw text response.
    """
    # === GEMINI branch ===
    if getattr(cb, "llm_type", None) == "gemini":
        try:
            pil_imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
            return cb.gemini_vision_llm.generate_content([prompt] + pil_imgs).text
        except Exception as e:
            raise RuntimeError(f"Gemini vision call failed: {e}")

    # === GPT / OpenAI branch ===
    elif getattr(cb, "llm_type", None) == "gpt":
        # make sure the client and model are configured
        if not hasattr(cb, "gpt_vision_client") or not hasattr(cb, "gpt_vision_llm"):
            raise RuntimeError("cb.llm_type=='gpt' but no OpenAI vision client/model on cb")
        try:
            content = [{"type": "text", "text": prompt}]
            for img in images:
                uri = "data:image/png;base64," + base64.b64encode(img).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": uri, "detail": detail}
                })
            msg = {"role": "user", "content": content}
            resp = cb.gpt_vision_client.chat.completions.create(
                model=cb.gpt_vision_llm,
                messages=[msg],
                # temperature=0
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            raise RuntimeError(f"OpenAI vision call failed: {e}")

    else:
        raise RuntimeError(f"Unsupported cb.llm_type={getattr(cb,'llm_type',None)} – must be 'gemini' or 'gpt'.")
#================================================================================================================


def extract_pdf_metadata(
    pdf_path: str,
    cb,
    num_pages: int = 2
) -> Dict[str, Any]:
    """
    Render the first `num_pages` of `pdf_path` as PNGs, send them to the vision LLM
    with a metadata prompt, and parse the JSON response.

    Returns a dict with any of: title, authors, publication_date, report_number, doi, keywords.
    Raises on network/JSON errors.
    """
    # 1) Rasterize
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF {pdf_path}: {e}")

    images: List[bytes] = []
    for i in range(min(num_pages, doc.page_count)):
        try:
            pix = doc.load_page(i).get_pixmap(dpi=200)
            images.append(pix.tobytes("png"))
        except Exception as e:
            # skip problem pages
            continue
    doc.close()

    if not images:
        raise RuntimeError(f"No pages rendered from {pdf_path}")

    # 2) Prompt
    prompt = (
        f"You will be shown up to {num_pages} images (PNG) of the front pages of a technical PDF.\n"
        "Extract as much of the following metadata as you can find, and return it as a pure JSON object with these keys:\n"
        "  • title (string)\n"
        "  • authors (array of strings)\n"
        "  • publication_date (YYYY-MM-DD if available)\n"
        "  • report_number (string)\n"
        "  • doi (string)\n"
        "  • keywords (array of short terms)\n\n"
        "Omit any field you cannot locate."
    )

    # 3) Call LLM
    raw = call_vision_llm(images, prompt, cb, detail="auto")

    # 4) Extract JSON substring
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError(f"No JSON found in LLM response:\n{raw}")
    candidate = raw[start : end+1].strip().strip("```").strip()

    # 5) Parse
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON from vision response:\n{candidate}\nError: {e}")

#======================================================================
# Logging syntax to write reasoning chain into a file
#======================================================================
import logging
from logging.handlers import RotatingFileHandler
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import AgentAction, AgentFinish, LLMResult

#======================================================================
# Hard-code nothing -> look for an env-var first, otherwise
# default to a sibling directory called "RADIANT_LLM_Logs".
#======================================================================
LOG_DIR = Path(
    os.getenv(
        "RADIANT_LLM_LOG_DIR",
        os.getenv(
            "DecodedAI_LOG_DIR",  # backward-compatible fallback
            Path.cwd() / "RADIANT_LLM_Logs",
        ),
    )
)
LOG_DIR.mkdir(parents=True, exist_ok=True)  # safe no-op if it already exists

def LLMReasoningChainLogger(model_name: str) -> logging.Logger:
    """
    Return a logger writing to RADIANT_LLM_Logs/<model>_reasoning_chain.log,
    rotating at 5 MiB with 3 backups.  Falls back to console on error.
    """
    logger = logging.getLogger(f"reasoning_{model_name}")
    logger.setLevel(logging.DEBUG)

    logfile = LOG_DIR / f"{model_name}_reasoning_chain.log"   # ← only line that changes
    try:
        # Ensure file exists immediately after model initialization (Dash-like behavior).
        logfile.parent.mkdir(parents=True, exist_ok=True)
        logfile.touch(exist_ok=True)

        # Rebind handlers each time to avoid stale file paths across reloads.
        for h in list(logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)

        handler = RotatingFileHandler(
            logfile,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "\n%(asctime)s %(levelname)s %(message)s"
        ))
        logger.addHandler(handler)
    except Exception as e:
        # Fallback to console if file handler fails
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter(
            "\n%(asctime)s %(levelname)s [LOGGER-ERROR] %(message)s"
        ))
        logger.handlers.clear()
        logger.addHandler(console)
        logger.error(f"Could not init RotatingFileHandler({logfile}): {e}")

    return logger


def appendStreamEventLog(event_type: str, payload: str) -> None:
    """
    Persist stream/query events for offline debugging/traceability.
    """
    try:
        log_file = LOG_DIR / "streaming_events.log"
        stamp = _dt.datetime.utcnow().isoformat()
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{event_type}\t{payload}\n")
    except Exception:
        # Never break runtime flow because of logging failures.
        pass

#======================================================================
class ReasoningLoggerCallback(BaseCallbackHandler):
    """
    A LangChain callback that logs each step of the agent:
      - on_llm_start/on_llm_end
      - on_agent_action
      - on_agent_finish

    Optionally, it can also stream these log lines into an in-memory
    list (events_sink) that can be exposed via an API for live
    \"thinking\" / reasoning visualizations in a UI.
    """

    def __init__(self, logger: logging.Logger, events_sink: Optional[List[str]] = None):
        self.logger = logger
        self.events_sink = events_sink
        self.step_count = 0

    def appendEvent(self, text: str) -> None:
        if self.events_sink is not None:
            self.events_sink.append(text)
        # Also persist every reasoning callback event to offline stream log.
        appendStreamEventLog("reasoning_event", text)

    def on_llm_start(self, serialized, prompts, **kwargs):
        # Ensure a blank line 
        # self.logger.info("") 
        try:
            msg = f"[LLM_START] prompts={prompts}"
            self.logger.info(msg)
            self.appendEvent(msg)
        except Exception as e:
            self.logger.error(f"[LLM_START logging failed] {e}")

    def on_llm_end(self, response: LLMResult, **kwargs):
        # Ensure a blank line 
        # self.logger.info("")
        try:
            msg = f"[LLM_END] {response.llm_output or ''}"
            self.logger.info(msg)
            self.appendEvent(msg)
        except Exception as e:
            self.logger.error(f"[LLM_END logging failed] {e}")

    def on_agent_action(self, action: AgentAction, **kwargs):
        self.step_count += 1
        try:
            msg = f"[AGENT_ACTION] {action.log}"
            self.logger.info(msg)
            self.appendEvent(msg)
        except Exception as e:
            self.logger.error(f"[AGENT_ACTION logging failed] {e}")

    def on_agent_finish(self, finish: AgentFinish, **kwargs):
         # Blank line + big separator + final message
        self.logger.info("") 
        try:
            msg = f"[AGENT_FINISH] {finish.log}"
            self.logger.info(msg)
            self.appendEvent(msg)
        except Exception as e:
            self.logger.error(f"[AGENT_FINISH logging failed] {e}")

    def on_tool_end(self, tool_end, **kwargs):
        msg = f"[TOOL_RESULT] {tool_end.output}"
        self.logger.info(msg)
        self.appendEvent(msg)

# =====================================================================
# =====================================================================
#                       YAML File reader 
# =====================================================================
import yaml
from pathlib import Path
from typing import Any, Tuple, Union
import traceback

def yaml_reader(path: Path, flatten: bool = True) -> Tuple[Union[Any, None], Union[str, None]]:
    """
    Loads a YAML file from a given path with robust error handling.
    If flatten is True, it converts the YAML content into a single string.
    Otherwise, it returns the raw Python dictionary.

    Returns:
        A tuple containing the loaded data (either a string or dict) and an error message.
        - If successful, returns (data, None).
        - If an error occurs, returns (None, error_message).
    """
    def flatten_yaml_to_string(yaml_dict):
        flattened_string = ""
        for key, value in yaml_dict.items():
            flattened_string += f"## {key.replace('_', ' ').title()}\n\n"
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    flattened_string += f"### {sub_key.replace('_', ' ').title()}\n"
                    if isinstance(sub_value, (str, int, float)):
                        flattened_string += f"{sub_value}\n\n"
                    elif isinstance(sub_value, list):
                        for item in sub_value:
                            if isinstance(item, dict):
                                flattened_string += f"- **{item.get('title', 'N/A')}:**\n"
                                if 'points' in item and isinstance(item['points'], list):
                                    for point in item['points']:
                                        flattened_string += f"  - {point}\n"
                                if 'description' in item and isinstance(item['description'], str):
                                    flattened_string += f"  - {item['description']}\n"
                            else:
                                flattened_string += f"- {item}\n"
                        flattened_string += "\n"
                    else:
                        flattened_string += f"{sub_value}\n\n"
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and 'description' in item:
                        flattened_string += f"- **{item.get('title', 'N/A')}:** {item['description']}\n"
                    elif isinstance(item, str):
                        flattened_string += f"- {item}\n"
                flattened_string += "\n"
            else:
                flattened_string += f"{value}\n\n"
            flattened_string += "---\n"
        return flattened_string.strip()

    try:
        if not path.exists():
            raise FileNotFoundError(f"The file was not found at the specified path: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            
        if data is None:
            data = {}

        if flatten:
            if not isinstance(data, dict):
                return (None, f"File '{path}' is not a valid YAML dictionary and cannot be flattened.")
            flattened_string = flatten_yaml_to_string(data)
            return (flattened_string, None)
        else:
            return (data, None)

    except FileNotFoundError as e:
        return (None, str(e))
    except yaml.YAMLError as e:
        error_message = f"YAML parsing error in file '{path}': {e}\n\nTraceback:\n{traceback.format_exc()}"
        return (None, error_message)
    except Exception as e:
        error_message = f"An unexpected error occurred while reading the file '{path}': {e}\n\nTraceback:\n{traceback.format_exc()}"
        return (None, error_message)
    

# #*********************** Try it ***********************
# # Path to your YAML file
# system_prompt_path = Path("system_prompt_autofluka.yml")

# # Test load
# system_prompt = yaml_reader(system_prompt_path)
# print(system_prompt)



#=====================================================================
#                       OSTI ID TILITY
# =====================================================================
###################################################################################
############################# Utility Functions End Here ##########################
###################################################################################
