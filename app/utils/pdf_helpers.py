# A Vitual Assitant for SAM Users: Based on LLM Augmentation and AI Agents. 
import os
import matplotlib.pyplot as plt
from difflib import get_close_matches
import base64
import openai
import pandas as pd
import warnings
import logging, itertools
import fitz  # PyMuPDF
from langchain.schema import Document
from langchain_openai import OpenAIEmbeddings
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from langchain_chroma import Chroma
import tempfile                          # To create a temposral directory for chromadb when running as an executable
from langchain.chains import RetrievalQAWithSourcesChain
from langchain.text_splitter import RecursiveCharacterTextSplitter # Import the text splitter

# Additional imports for LangChain agent setup
import webbrowser
import subprocess
import datetime as _dt
import shlex    # For SAM execution 
from tqdm import tqdm  # For SAM execution on cluster
from langchain_experimental.utilities import PythonREPL
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain.agents import create_tool_calling_agent, create_openai_tools_agent, AgentExecutor
from langchain_openai import ChatOpenAI

# Memory capabilities 
from langchain.prompts import MessagesPlaceholder
from langchain.memory import ConversationBufferMemory
from langchain.agents import initialize_agent
from langchain_community.agent_toolkits.load_tools import load_tools

from typing import  Annotated, Sequence
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
from collections import defaultdict, Counter
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
import re
import time
import pytesseract
import heapq

# Dash Components 
import dash
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc

# WorkSataion Execution
import paramiko


# Custom Utilities
from utils.nougat_helpers import NougatInitializer, RasterizePaper, StoppingCriteriaScores
from utils.general_utilities import GeneralAlerts
from utils.general_utilities import extract_pdf_metadata
from utils.general_utilities import call_vision_llm, extract_pdf_metadata

from langchain.retrievers import MergerRetriever
import json, os, logging, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Union, Tuple
import re

# =========================================================
# PDF source-scoped retrieval helpers
# =========================================================
# PDF filename detector for user queries.
# Intentionally DOES NOT allow spaces to avoid capturing phrases like
# "summarize ... RADIANT_LLM.pdf" as a single "filename".
# If you need to target PDFs with spaces in the name, pass `pdf_name=` explicitly.
PDF_NAME_PATTERN = re.compile(r"(?i)(?<![\\w.\\-])([\\w][\\w.\\-]{0,180}\\.pdf)(?![\\w.\\-])")


# Shared Visual Description Prompt
FIGURE_PROMPT = (
    "You are a specialized Nuclear Engineering Vision Analyst. You are viewing a page from a technical document. "
    "Your goal is to extract high-fidelity structured data from visual elements for a Retrieval-Augmented Generation (RAG) system. "
    "Your output must be precise, quantitative, and strictly follow the structure defined below.\n\n"

    "**PHASE 1: VISUAL SUPREMACY PROTOCOL (CRITICAL)**\n"
    "- **Discrepancy Detection**: You are required to explicitly check if the visual data matches the surrounding text claims.\n"
    "- **Trust the Pixels**: If the image shows a dimension label (e.g., '6'), and the text says something else (e.g., '5'), "
    "you must record the image value ('6') and report the discrepancy.\n\n"

    "**PHASE 2: STRUCTURAL ANALYSIS**\n"
    "For each distinct scientific visual (plot, chart, schematic, diagram), generate a description using STRICTLY the following five headings.\n\n"

    "- A **Figure** is defined as a visual element sharing a single figure number or caption "
    "(e.g., 'Figure 3'), even if it contains multiple panels or subplots.\n"
    "- If a single Figure contains mixed content (e.g., a schematic and a plot), "
    "describe all panels together as ONE Figure in a single description.\n"
    "- If no explicit figure number or caption is visible, treat a visually unified group of panels (shared axes, alignment, framing, or common visual context) as ONE Figure and identify it with the corresponding page number.\n\n"
    "1. **Subject**: A concise title or classification of the visual "
    "(e.g., 'Vertical Parabolic Gate Schematic', 'PWR Primary Loop P&ID', 'Decay Heat vs Time Plot').\n"
    "2. **Geometry & Labels**:\n"
    "   - Describe the shapes, layout, and components "
    "(e.g., 'U-shaped parabola opening upward', '3x3 grid of fuel pins').\n"
    "   - List meaningful text labels found *inside* the figure VERBATIM "
    "(e.g., 'Labels: Point A, Point B, Inlet Valve V-101').\n"
    "   - For Schematics: Describe connectivity "
    "(e.g., 'Pump discharges to Heat Exchanger').\n"
    "3. **Dimensions & Data (Quantitative)**:\n"
    "   - **Schematics**: Extract all physical dimension lines, radii, diameters, lengths, "
    "thicknesses, angles, and tolerances explicitly labeled in the figure "
    "(e.g., 'Inner radius = 1.2 m', 'Shield thickness = 0.15 m').\n"
    "   - **Plots/Charts (CRITICAL)**:\n"
    "       * Extract **axis variables**, **units**, and **numerical ranges** (min/max).\n"
    "       * Identify and quantify **key features**: peaks, minima, plateaus, inflection points, "
    "step changes, oscillations, or discontinuities and infer numerical values or ranges from the axes "
    "if not directly printed on the plot. DO NOT guess if values are not clearly visible.\n"
    "       * Describe **temporal or parametric trends explicitly** using quantitative language:\n"
    "           - e.g., 'Monotonic increase from 0–20 s',\n"
    "           - 'Exponential decay after shutdown',\n"
    "           - 'Prompt jump followed by delayed rise',\n"
    "           - 'Asymptotic stabilization near 600 MW',\n"
    "           - 'Oscillatory behavior with decreasing amplitude'.\n"
    "       * If multiple curves are present, distinguish them by **legend labels**, "
    "**line style**, or **color** (verbatim).\n"
    "       * If values are approximate due to resolution or scale, explicitly state this "
    "(e.g., '≈', 'estimated from plot').\n"
    "4. **Context**: Summarize the scientific purpose based on the surrounding page text "
    "(e.g., 'Used for hydrostatic pressure calculation in Exercise 1').\n"
    "5. **Discrepancy Check**: State if visual labels contradict text "
    "(e.g., 'ALERT: Image lists height as 6, but text caption says 5.'). "
    "If none, state 'No discrepancies detected'.\n\n"

    "**OUTPUT FORMAT**\n\n"
    "**IMPORTANT NOTE**:\n"
    "  - Return a strictly valid JSON list.\n"
    "  - You MUST return one JSON object per Figure present on the input page image.\n"
    "  - If a page contains multiple Figures (e.g., Figure 1, Figure 2, ..., Figure N), "
    "return one JSON object per Figure.\n"
    "  - If a Figure contains subplots (e.g., (a), (b), left/right, top/bottom panels), "
    "YOU MUST return ONE description per Figure, NOT per subplot.\n"
    "  - If a page contains no scientific visual, return an EMPTY JSON LIST: [].\n"
    "  - **Do NOT skip pages.**\n\n"

    "[\n"
    "  { \"description\": \"**Subject:** [Title]\\n"
    "**Geometry & Labels:** [Detailed description]\\n"
    "**Dimensions & Data:** [Quantitative extraction]\\n"
    "**Context:** [Purpose]\\n"
    "**Discrepancy Check:** [Result]\" },\n"
    "  { \"description\": \"...\" }\n"
    "]"
)


def normalize_pdf_basename(name: str) -> str:
    if not name:
        return ""
    name = name.strip().strip("\"'“”‘’`")
    name = name.strip().strip(".,;:)]}>")
    return os.path.basename(name)


def infer_pdf_sources_from_query(
                                query: str,
                                explicit_pdf_name: Optional[str],
                                working_directory: Optional[str],
                            ) -> List[str]:
    candidates: List[str] = []

    if explicit_pdf_name:
        candidates.append(normalize_pdf_basename(explicit_pdf_name))

    if query:
        for match in PDF_NAME_PATTERN.findall(query):
            candidates.append(normalize_pdf_basename(match))

    candidates = [c for c in candidates if c.lower().endswith(".pdf")]
    if not candidates:
        return []

    # Deduplicate while preserving order
    seen = set()
    unique: List[str] = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            unique.append(c)
            seen.add(key)

    if not working_directory or not os.path.isdir(working_directory):
        return unique

    try:
        existing = {
            os.path.basename(p)
            for root, _, files in os.walk(working_directory)
            for p in [os.path.join(root, f) for f in files]
            if p.lower().endswith(".pdf")
        }
        if not existing:
            return unique
        existing_lower = {e.lower() for e in existing}
        filtered = [u for u in unique if u.lower() in existing_lower]
        # If none of the inferred candidates exist in the directory, do NOT apply a filter.
        # Returning the non-existent candidates causes retrieval to collapse and encourages hallucinations.
        return filtered
    except Exception:
        return unique


def vector_store_similarity_search(vs, query: str, k: int, metadata_filter: Optional[Dict[str, Any]]):
    """
    Best-effort similarity search with optional metadata filtering across LangChain/Chroma versions.
    """
    if not metadata_filter:
        return vs.similarity_search(query, k=k)

    # Newer LangChain wrappers typically accept `filter=`; some accept `where=`.
    try:
        return vs.similarity_search(query, k=k, filter=metadata_filter)
    except TypeError:
        try:
            return vs.similarity_search(query, k=k, where=metadata_filter)
        except TypeError:
            return vs.similarity_search(query, k=k)

# ==============================================================================================================
#  Building Vector Store as Retriever: Putting al the above functions together
# ==============================================================================================================

def build_vector_store_retriever(vector_store, k: int = 20, metadata_filter: Optional[Dict[str, Any]] = None):
    """
    Return a LangChain BaseRetriever from a vector store (avoids Pydantic validation errors).

    We prefer using `vector_store.as_retriever(search_kwargs=...)` because LangChain expects
    a BaseRetriever instance (not an arbitrary Python object).

    The filter key varies by LangChain/Chroma versions (`filter` vs `where`), so we try both.
    """
    search_kwargs: Dict[str, Any] = {"k": k}

    # If a metadata filter is requested, enforce it even if the backend doesn't support `filter`/`where`.
    # This avoids cross-document contamination (e.g., the “English law” snippet coming from another PDF).
    if metadata_filter:
        # Derive allowed sources for a safe, client-side post-filter fallback.
        allowed_sources = None
        try:
            src = metadata_filter.get("source")
            if isinstance(src, str):
                allowed_sources = {src.lower()}
            elif isinstance(src, dict) and "$in" in src and isinstance(src["$in"], list):
                allowed_sources = {str(s).lower() for s in src["$in"]}
        except Exception:
            allowed_sources = None

        BaseRetriever = None
        try:
            from langchain_core.retrievers import BaseRetriever as _BaseRetriever  # type: ignore
            BaseRetriever = _BaseRetriever
        except Exception:
            try:
                from langchain.schema import BaseRetriever as _BaseRetriever  # type: ignore
                BaseRetriever = _BaseRetriever
            except Exception:
                try:
                    from langchain.schema.retriever import BaseRetriever as _BaseRetriever  # type: ignore
                    BaseRetriever = _BaseRetriever
                except Exception:
                    BaseRetriever = None

        if BaseRetriever is not None:
            class FilteredVectorStoreRetriever(BaseRetriever):  # type: ignore[misc,valid-type]
                vector_store: Any
                k: int = 20
                metadata_filter: Optional[Dict[str, Any]] = None
                allowed_sources: Optional[set] = None

                def _get_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
                    docs = []

                    # Try server-side filtering first (filter/where), then always post-filter if we can.
                    try:
                        docs = vector_store_similarity_search(
                            self.vector_store,
                            query=query,
                            k=max(self.k * 5, self.k),
                            metadata_filter=self.metadata_filter,
                        )
                    except Exception:
                        docs = self.vector_store.similarity_search(query, k=max(self.k * 5, self.k))

                    if self.allowed_sources:
                        docs = [
                            d for d in docs
                            if str(d.metadata.get("source", "")).lower() in self.allowed_sources
                        ]

                    return docs[: self.k]

                async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
                    return self._get_relevant_documents(query, run_manager=run_manager)

            return FilteredVectorStoreRetriever(
                vector_store=vector_store,
                k=k,
                metadata_filter=metadata_filter,
                allowed_sources=allowed_sources,
            )

        # If we can't build a BaseRetriever, fall back to an unfiltered retriever (best effort).
        # This may reintroduce cross-document hits, so we at least try both filter keys.
        try:
            return vector_store.as_retriever(search_kwargs={**search_kwargs, "filter": metadata_filter})
        except TypeError:
            try:
                return vector_store.as_retriever(search_kwargs={**search_kwargs, "where": metadata_filter})
            except TypeError:
                return vector_store.as_retriever(search_kwargs=search_kwargs)

    # No filter requested: standard retriever.
    try:
        return vector_store.as_retriever(search_kwargs=search_kwargs)
    except TypeError:
        return vector_store.as_retriever()


# ==============================================================================================================
# Retrieval artifact filtering / formatting helpers
# ==============================================================================================================

ARTIFACT_FALLBACK_MESSAGE = (
    "I can’t determine this from the currently retrievable PDF content because "
    "the available extraction for the relevant page or figure is dominated by unrelated artifact text."
)

CHUNKS_KB_FILENAME = "01_chunks_kb.jsonl"
VISUALS_KB_FILENAME = "02_visuals_kb.jsonl"
METADATA_KB_FILENAME = "03_metadata_kb.jsonl"

VISUAL_QUERY_TERMS = {
    "figure", "fig", "visual", "diagram", "schematic", "flowchart",
    "caption", "oval", "arrow", "table", "plot", "graph", "image",
}

SEARCH_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "from",
    "what", "which", "who", "whom", "where", "when", "why", "how", "is",
    "are", "was", "were", "be", "been", "being", "with", "by", "do", "does",
    "did", "this", "that", "these", "those", "it", "its", "as", "at", "into",
    "after", "before", "under", "over", "between", "using", "use", "please",
    "provide", "exact", "text", "content", "pdf", "page",
}

ARTIFACT_PATTERNS = [
    re.compile(r"contract\s+is\s+governed\s+by\s+english\s+law", re.IGNORECASE),
    re.compile(r"governed\s+by\s+english\s+law", re.IGNORECASE),
    re.compile(r"english\s+law\s*\(england\)", re.IGNORECASE),
    re.compile(r"controls?\s+its\s+interpretation", re.IGNORECASE),
]

LEGAL_QUERY_TERMS = {
    "english law",
    "governed by",
    "contract",
    "legal",
    "clause",
    "interpretation",
}

NON_ANSWER_PATTERNS = [
    re.compile(r"\bi\s+do\s+not\s+know\b", re.IGNORECASE),
    re.compile(r"\bi\s+don[’']?t\s+know\b", re.IGNORECASE),
    re.compile(r"\bi\s+can(?:not|'t|’t)\s+determine\b", re.IGNORECASE),
    re.compile(r"\bnot\s+retrievable\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+include\b", re.IGNORECASE),
    re.compile(r"\bnot\s+available\b", re.IGNORECASE),
]


def query_targets_legal_text(query: Optional[str]) -> bool:
    normalized = re.sub(r"\s+", " ", (query or "")).strip().lower()
    if not normalized:
        return False
    return any(term in normalized for term in LEGAL_QUERY_TERMS)


def normalize_matchable_text(text: Optional[str]) -> str:
    normalized = re.sub(r"[*_`#>\[\]\(\)~]", " ", (text or ""))
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def detect_pdf_artifact_text(text: Optional[str]) -> Optional[str]:
    normalized = normalize_matchable_text(text)
    if not normalized:
        return None

    for pattern in ARTIFACT_PATTERNS:
        match = pattern.search(normalized)
        if match:
            start = max(0, match.start() - 80)
            end = min(len(normalized), match.end() + 120)
            return normalized[start:end].strip(" -:;,.")[:320]
    return None


def answer_is_artifact_dominated(answer: Optional[str]) -> bool:
    return detect_pdf_artifact_text(answer) is not None


def answer_is_non_answer(answer: Optional[str]) -> bool:
    normalized = normalize_matchable_text(answer).lower()
    if not normalized:
        return True
    return any(pattern.search(normalized) for pattern in NON_ANSWER_PATTERNS)


def dedupe_documents(documents: List[Document]) -> List[Document]:
    unique_docs: List[Document] = []
    seen = set()

    for doc in documents or []:
        metadata = doc.metadata or {}
        signature = (
            str(metadata.get("source", "")),
            str(metadata.get("page", "")),
            str(metadata.get("chunk_index", "")),
            str(metadata.get("figure_id", "")),
            str(metadata.get("figure_index", "")),
            re.sub(r"\s+", " ", (doc.page_content or "")).strip(),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique_docs.append(doc)

    return unique_docs


def split_artifact_documents(
    documents: List[Document],
    query: Optional[str] = None,
) -> Tuple[List[Document], List[Dict[str, Any]]]:
    if query_targets_legal_text(query):
        return dedupe_documents(documents or []), []

    usable_docs: List[Document] = []
    artifact_entries: List[Dict[str, Any]] = []

    for doc in dedupe_documents(documents or []):
        snippet = detect_pdf_artifact_text(doc.page_content)
        if snippet:
            artifact_entries.append({"document": doc, "snippet": snippet})
        else:
            usable_docs.append(doc)

    return usable_docs, artifact_entries


def get_relevant_documents(retriever, query: str) -> List[Document]:
    if hasattr(retriever, "invoke"):
        docs = retriever.invoke(query)
    elif hasattr(retriever, "get_relevant_documents"):
        docs = retriever.get_relevant_documents(query)
    else:
        docs = []

    return [doc for doc in (docs or []) if isinstance(doc, Document)]


def build_static_retriever(documents: List[Document]):
    BaseRetriever = None
    try:
        from langchain_core.retrievers import BaseRetriever as _BaseRetriever  # type: ignore
        BaseRetriever = _BaseRetriever
    except Exception:
        try:
            from langchain.schema import BaseRetriever as _BaseRetriever  # type: ignore
            BaseRetriever = _BaseRetriever
        except Exception:
            try:
                from langchain.schema.retriever import BaseRetriever as _BaseRetriever  # type: ignore
                BaseRetriever = _BaseRetriever
            except Exception:
                BaseRetriever = None

    if BaseRetriever is None:
        raise RuntimeError("BaseRetriever is unavailable; cannot build static retriever.")

    class StaticDocumentRetriever(BaseRetriever):  # type: ignore[misc,valid-type]
        docs: List[Document]

        def _get_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
            return list(self.docs)

        async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
            return list(self.docs)

    return StaticDocumentRetriever(docs=list(documents or []))


def format_source_info(doc: Document) -> str:
    metadata = doc.metadata or {}
    source_info = f"Source: {metadata.get('source', 'Unknown')}"

    if "page" in metadata:
        source_info += f", Page: {metadata['page']}"

    doc_type = metadata.get("doc_type")
    if doc_type == "text" and "chunk_index" in metadata:
        source_info += f", Chunk: {metadata['chunk_index']}"
    elif "chunk_index" in metadata:
        source_info += f", Chunk: {metadata['chunk_index']}"
    elif doc_type == "figure":
        if "figure_id" in metadata:
            source_info += f", Figure: {metadata['figure_id']}"
        elif "figure_index" in metadata:
            source_info += f", Figure Index: {metadata['figure_index']}"

    if "title" in metadata and metadata["title"] not in source_info:
        source_info += f" (Title: {metadata['title']})"
    if "authors" in metadata and metadata["authors"] not in source_info:
        try:
            source_info += f" (Authors: {', '.join(metadata['authors'])})"
        except Exception:
            pass

    return source_info


def format_sources(documents: List[Document], empty_message: str = "[No usable sources listed]") -> str:
    docs = dedupe_documents(documents or [])
    if not docs:
        return empty_message
    return "\n".join(format_source_info(doc) for doc in docs)


def collect_source_counts(documents: List[Document]) -> Counter:
    counts: Counter = Counter()
    for doc in dedupe_documents(documents or []):
        source = str((doc.metadata or {}).get("source", "")).strip()
        if source:
            counts[source] += 1
    return counts


def answer_has_ambiguous_sources(
    source_documents: List[Document],
    query: Optional[str],
    explicit_pdf_name: Optional[str] = None,
) -> bool:
    if explicit_pdf_name or not query_prefers_visual_search(query):
        return False

    counts = collect_source_counts(source_documents)
    if len(counts) <= 1:
        return False

    total = sum(counts.values())
    dominant = counts.most_common(1)[0][1]
    return dominant / max(total, 1) < 0.6


def select_documents_for_qa(
    documents: List[Document],
    query: Optional[str],
    max_docs: int = 8,
) -> List[Document]:
    docs = dedupe_documents(documents or [])
    if len(docs) <= max_docs:
        return docs

    prefers_visuals = query_prefers_visual_search(query)

    def _priority(item: Tuple[int, Document]) -> Tuple[int, int]:
        index, doc = item
        metadata = doc.metadata or {}
        doc_type = metadata.get("doc_type")
        score = 0
        if prefers_visuals and doc_type == "figure":
            score += 3
        elif prefers_visuals and doc_type == "text":
            score += 1
        if metadata.get("page") is not None:
            score += 1
        return (-score, index)

    ranked = sorted(enumerate(docs), key=_priority)
    selected = [doc for _, doc in ranked[:max_docs]]
    return dedupe_documents(selected)


def resolve_visual_kb_paths(working_directory: str) -> List[Path]:
    path = Path(working_directory) / VISUALS_KB_FILENAME
    return [path] if path.exists() else []


def resolve_visual_kb_path(working_directory: str) -> Optional[Path]:
    paths = resolve_visual_kb_paths(working_directory)
    return paths[0] if paths else None


def query_prefers_visual_search(query: Optional[str]) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    return any(token in VISUAL_QUERY_TERMS for token in tokens)


def resolve_kb_search_paths(working_directory: str, query: Optional[str]) -> List[Tuple[str, Path]]:
    workdir = Path(working_directory)
    chunks_path = workdir / CHUNKS_KB_FILENAME
    metadata_path = workdir / METADATA_KB_FILENAME
    visual_paths = resolve_visual_kb_paths(working_directory)
    prefers_visuals = query_prefers_visual_search(query)

    ordered: List[Tuple[str, Path]] = []
    if prefers_visuals:
        ordered.extend(("visuals", path) for path in visual_paths)
        if chunks_path.exists():
            ordered.append(("chunks", chunks_path))
    else:
        if chunks_path.exists():
            ordered.append(("chunks", chunks_path))
        ordered.extend(("visuals", path) for path in visual_paths)

    if metadata_path.exists():
        ordered.append(("metadata", metadata_path))

    return ordered


def _extract_query_terms(query: Optional[str]) -> List[str]:
    terms = []
    for token in re.findall(r"[a-z0-9_.:/-]+", (query or "").lower()):
        if len(token) <= 1:
            continue
        if token in SEARCH_STOPWORDS:
            continue
        terms.append(token)
    return terms


def _extract_query_phrases(query: Optional[str]) -> List[str]:
    text = (query or "").lower()
    phrases: List[str] = []

    for match in re.findall(r'"([^"]+)"|\'([^\']+)\'', text):
        phrase = next((part.strip() for part in match if part.strip()), "")
        if phrase:
            phrases.append(phrase)

    for pattern in (
        r"\bfigure\s+\d+[a-z]?\b",
        r"\bpage\s+\d+\b",
        r"\bsection\s+[a-z0-9.\-]+\b",
    ):
        phrases.extend(m.group(0) for m in re.finditer(pattern, text))

    deduped: List[str] = []
    seen = set()
    for phrase in phrases:
        if phrase not in seen:
            seen.add(phrase)
            deduped.append(phrase)
    return deduped


def _extract_query_pages(query: Optional[str]) -> set:
    return {
        int(match.group(1))
        for match in re.finditer(r"\bpage\s+(\d+)\b", (query or "").lower())
    }


def _normalize_search_text(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _metadata_summary(record: Dict[str, Any]) -> str:
    parts = []
    for key in ("title", "subtitle", "summary", "abstract", "doi"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    authors = record.get("authors")
    if isinstance(authors, list):
        parts.append(", ".join(str(author).strip() for author in authors if str(author).strip()))
    elif isinstance(authors, str) and authors.strip():
        parts.append(authors.strip())

    return " | ".join(part for part in parts if part)


def _record_text_and_type(record: Dict[str, Any], kb_kind: str) -> Tuple[str, str]:
    if kb_kind == "chunks":
        return (record.get("content") or "").strip(), "text"
    if kb_kind == "visuals":
        return (record.get("description") or record.get("caption") or "").strip(), "figure"
    return _metadata_summary(record), "metadata"


def _score_jsonl_record(
    record: Dict[str, Any],
    kb_kind: str,
    query: str,
    query_terms: List[str],
    query_phrases: List[str],
    query_pages: set,
    allowed_sources: Optional[set],
    priority_index: int,
) -> Optional[Dict[str, Any]]:
    source = str(record.get("source") or "").strip()
    source_base = Path(source).name
    if allowed_sources and source_base.lower() not in allowed_sources:
        return None

    main_text, record_type = _record_text_and_type(record, kb_kind)
    searchable_text = _normalize_search_text(
        " ".join(
            filter(
                None,
                [
                    source_base,
                    main_text,
                    str(record.get("figure_id") or ""),
                    str(record.get("chunk_id") or ""),
                    str(record.get("title") or ""),
                ],
            )
        )
    )
    if not searchable_text:
        return None

    normalized_query = _normalize_search_text(query)
    score = 0.0
    matched_terms = 0

    exact_query_match = bool(normalized_query and normalized_query in searchable_text)
    if exact_query_match:
        score += 12.0

    phrase_match_count = 0
    for phrase in query_phrases:
        if phrase and phrase in searchable_text:
            phrase_match_count += 1
            score += min(5.0, 2.0 + 0.5 * len(phrase.split()))

    for term in query_terms:
        if term in searchable_text:
            matched_terms += 1
            score += 1.0

    if query_terms:
        score += matched_terms / max(len(query_terms), 1)

    record_page = record.get("page")
    try:
        page_int = int(record_page)
    except Exception:
        page_int = None

    if page_int is not None and page_int in query_pages:
        score += 2.5

    if query_prefers_visual_search(query) and record_type == "figure":
        score += 1.5
    elif query_prefers_visual_search(query) and record_type == "metadata":
        score -= 0.25

    if source_base and source_base.lower() in normalized_query:
        score += 2.0

    score += max(0.0, 0.6 - (0.1 * priority_index))

    if score <= 0 or (matched_terms == 0 and phrase_match_count == 0 and not exact_query_match):
        return None

    excerpt = re.sub(r"\s+", " ", main_text).strip()
    excerpt = excerpt[:360] if excerpt else "[No excerpt available]"

    metadata = {
        "source": source_base or source,
        "page": record.get("page"),
        "document_id": record.get("document_id"),
        "doc_type": record_type,
        "kb_file": record.get("kb_file"),
    }

    if record_type == "text":
        metadata["chunk_index"] = record.get("chunk_index")
        metadata["chunk_id"] = record.get("chunk_id")
    elif record_type == "figure":
        metadata["figure_index"] = record.get("figure_index")
        metadata["figure_id"] = record.get("figure_id")
    else:
        if record.get("title"):
            metadata["title"] = record.get("title")
        if record.get("authors"):
            metadata["authors"] = record.get("authors")

    document = Document(page_content=main_text or excerpt, metadata=metadata)
    dedupe_key = (
        metadata.get("source"),
        metadata.get("page"),
        metadata.get("chunk_id"),
        metadata.get("figure_id"),
        metadata.get("title"),
        metadata.get("kb_file"),
    )

    return {
        "score": score,
        "record_type": record_type,
        "excerpt": excerpt,
        "document": document,
        "dedupe_key": dedupe_key,
    }


def direct_jsonl_kb_search(
    working_directory: str,
    query: str,
    pdf_name: Optional[str] = None,
    max_hits: int = 8,
) -> Dict[str, Any]:
    search_paths = resolve_kb_search_paths(working_directory, query)
    query_terms = _extract_query_terms(query)
    query_phrases = _extract_query_phrases(query)
    query_pages = _extract_query_pages(query)

    requested_sources = infer_pdf_sources_from_query(query, pdf_name, working_directory)
    allowed_sources = {source.lower() for source in requested_sources} if requested_sources else None

    heap: List[Tuple[float, int, Dict[str, Any]]] = []
    counter = 0
    seen_keys = set()

    for priority_index, (kb_kind, path) in enumerate(search_paths):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(record, dict):
                        continue

                    record["kb_file"] = path.name
                    scored = _score_jsonl_record(
                        record=record,
                        kb_kind=kb_kind,
                        query=query,
                        query_terms=query_terms,
                        query_phrases=query_phrases,
                        query_pages=query_pages,
                        allowed_sources=allowed_sources,
                        priority_index=priority_index,
                    )
                    if not scored:
                        continue

                    dedupe_key = scored["dedupe_key"]
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)

                    hit = {
                        "score": scored["score"],
                        "record_type": scored["record_type"],
                        "excerpt": scored["excerpt"],
                        "document": scored["document"],
                        "kb_file": path.name,
                        "line_number": line_number,
                    }

                    counter += 1
                    if len(heap) < max_hits:
                        heapq.heappush(heap, (hit["score"], counter, hit))
                    elif hit["score"] > heap[0][0]:
                        heapq.heapreplace(heap, (hit["score"], counter, hit))
        except OSError:
            continue

    hits = [item[2] for item in sorted(heap, key=lambda item: (-item[0], item[1]))]
    documents = [hit["document"] for hit in hits if isinstance(hit.get("document"), Document)]

    return {
        "query": query,
        "pdf_name": pdf_name,
        "searched_files": [str(path.name) for _, path in search_paths],
        "requested_sources": requested_sources,
        "hits": hits,
        "documents": dedupe_documents(documents),
    }


def rank_jsonl_source_candidates(
    search_result: Optional[Dict[str, Any]],
    query: Optional[str],
) -> List[Dict[str, Any]]:
    if not search_result:
        return []

    prefers_visuals = query_prefers_visual_search(query)
    grouped: Dict[str, Dict[str, Any]] = {}

    for hit in search_result.get("hits") or []:
        doc = hit.get("document")
        if not isinstance(doc, Document):
            continue
        metadata = doc.metadata or {}
        source = str(metadata.get("source", "")).strip()
        if not source:
            continue

        entry = grouped.setdefault(
            source,
            {
                "source": source,
                "hits": [],
                "documents": [],
                "top_score": 0.0,
                "score_sum": 0.0,
                "visual_hits": 0,
                "pages": set(),
            },
        )
        entry["hits"].append(hit)
        entry["documents"].append(doc)
        entry["top_score"] = max(entry["top_score"], float(hit.get("score", 0.0)))
        entry["score_sum"] += float(hit.get("score", 0.0))
        if metadata.get("doc_type") == "figure":
            entry["visual_hits"] += 1
        if metadata.get("page") is not None:
            entry["pages"].add(metadata.get("page"))

    ranked: List[Dict[str, Any]] = []
    for entry in grouped.values():
        router_score = (
            entry["top_score"] * 2.0
            + entry["score_sum"] * 0.35
            + min(len(entry["hits"]), 3) * 0.5
            + (0.75 if prefers_visuals and entry["visual_hits"] else 0.0)
        )
        ranked.append(
            {
                "source": entry["source"],
                "router_score": router_score,
                "top_score": entry["top_score"],
                "hit_count": len(entry["hits"]),
                "visual_hits": entry["visual_hits"],
                "pages": sorted(entry["pages"]),
                "documents": select_documents_for_qa(entry["documents"], query, max_docs=8),
            }
        )

    ranked.sort(key=lambda item: (-item["router_score"], -item["top_score"], -item["hit_count"], item["source"]))
    return ranked


def source_routing_is_ambiguous(source_candidates: List[Dict[str, Any]]) -> bool:
    if len(source_candidates) < 2:
        return False

    top = float(source_candidates[0].get("router_score", 0.0))
    second = float(source_candidates[1].get("router_score", 0.0))
    if top <= 0:
        return False

    return second >= max(top * 0.88, top - 1.0)


def select_primary_routed_source(
    search_result: Optional[Dict[str, Any]],
    query: Optional[str],
) -> Dict[str, Any]:
    candidates = rank_jsonl_source_candidates(search_result, query)
    return {
        "primary_source": candidates[0]["source"] if candidates else None,
        "candidate_sources": [candidate["source"] for candidate in candidates],
        "candidates": candidates,
        "ambiguous": source_routing_is_ambiguous(candidates),
    }


def select_routed_source_documents(
    search_result: Optional[Dict[str, Any]],
    source: Optional[str],
    query: Optional[str],
    max_docs: int = 6,
) -> List[Document]:
    if not search_result or not source:
        return []

    docs = []
    for hit in search_result.get("hits") or []:
        doc = hit.get("document")
        if not isinstance(doc, Document):
            continue
        if str((doc.metadata or {}).get("source", "")).strip() != source:
            continue
        docs.append(doc)

    return select_documents_for_qa(docs, query, max_docs=max_docs)


def _score_documents_against_answer(
    documents: List[Document],
    answer: Optional[str],
    query: Optional[str],
) -> float:
    normalized_answer = _normalize_search_text(answer)
    if not normalized_answer:
        return 0.0

    answer_terms = _extract_query_terms(answer)[:12]
    answer_phrases = _extract_query_phrases(answer)
    best_score = 0.0

    for doc in dedupe_documents(documents or []):
        searchable = _normalize_search_text(doc.page_content)
        if not searchable:
            continue

        score = 0.0
        if normalized_answer and normalized_answer in searchable:
            score += 4.0

        for phrase in answer_phrases:
            if phrase and phrase in searchable:
                score += 2.0

        term_matches = sum(1 for term in answer_terms if term in searchable)
        score += min(term_matches, 6) * 0.5

        if query_prefers_visual_search(query) and (doc.metadata or {}).get("doc_type") == "figure":
            score += 0.5

        best_score = max(best_score, score)

    return best_score


def select_corroborating_documents(
    search_result: Optional[Dict[str, Any]],
    primary_source: Optional[str],
    answer: Optional[str],
    query: Optional[str],
    max_sources: int = 2,
    max_docs_per_source: int = 1,
) -> List[Document]:
    if not search_result or not primary_source or answer_is_non_answer(answer) or answer_is_artifact_dominated(answer):
        return []

    corroborating: List[Document] = []
    added_sources = 0

    for candidate in rank_jsonl_source_candidates(search_result, query):
        source = candidate.get("source")
        if not source or source == primary_source:
            continue

        candidate_docs = select_routed_source_documents(
            search_result,
            source,
            query,
            max_docs=max_docs_per_source,
        )
        support_score = _score_documents_against_answer(candidate_docs, answer, query)
        if support_score < 1.5:
            continue

        corroborating.extend(candidate_docs[:max_docs_per_source])
        added_sources += 1
        if added_sources >= max_sources:
            break

    return dedupe_documents(corroborating)


def build_debug_raw_kb_section(
    search_result: Optional[Dict[str, Any]],
    max_entries: int = 3,
) -> str:
    if not search_result:
        return ""

    searched_files = search_result.get("searched_files") or []
    hits = search_result.get("hits") or []
    lines = ["**Debug raw KB fallback**:"]
    lines.append(f"- Searched KB files: {', '.join(searched_files) if searched_files else '[none]'}")

    if not hits:
        lines.append("- No useful raw KB hits found.")
        return "\n".join(lines)

    lines.append(f"- Useful raw KB hits found: {len(hits)}")
    for hit in hits[:max_entries]:
        doc = hit.get("document")
        if not isinstance(doc, Document):
            continue
        lines.append(
            f"- [{hit.get('record_type', 'unknown')}, score {hit.get('score', 0.0):.2f}] "
            f"{hit.get('excerpt', '[No excerpt available]')}"
        )
        lines.append(f"  {format_source_info(doc)} | KB: {hit.get('kb_file')} | Line: {hit.get('line_number')}")

    return "\n".join(lines)


def format_jsonl_search_results(
    search_result: Dict[str, Any],
    max_entries: int = 8,
) -> str:
    hits = search_result.get("hits") or []
    lines = ["**Direct JSONL KB Search Results**:"]
    lines.append(f"- Searched KB files: {', '.join(search_result.get('searched_files') or ['[none]'])}")

    if search_result.get("requested_sources"):
        lines.append(f"- Source filter: {', '.join(search_result['requested_sources'])}")

    if not hits:
        lines.append("- No useful raw KB hits found.")
        return "\n".join(lines)

    for index, hit in enumerate(hits[:max_entries], start=1):
        doc = hit.get("document")
        source_line = format_source_info(doc) if isinstance(doc, Document) else "Source: Unknown"
        lines.append(
            f"{index}. [{hit.get('record_type', 'unknown')}, score {hit.get('score', 0.0):.2f}] "
            f"{hit.get('excerpt', '[No excerpt available]')}"
        )
        lines.append(f"   {source_line} | KB: {hit.get('kb_file')} | Line: {hit.get('line_number')}")

    return "\n".join(lines)


def build_debug_artifact_section(
    artifact_entries: List[Dict[str, Any]],
    max_entries: int = 3,
) -> str:
    if not artifact_entries:
        return ""

    lines = ["**Debug artifact excerpt**:"]
    for entry in artifact_entries[:max_entries]:
        doc = entry.get("document")
        snippet = entry.get("snippet", "").strip()
        if not isinstance(doc, Document):
            continue
        lines.append(f"- Snippet: {snippet}")
        lines.append(f"  {format_source_info(doc)}")

    return "\n".join(lines)


def build_retrieval_response(
    answer: Optional[str],
    source_documents: List[Document],
    corroborating_documents: Optional[List[Document]] = None,
    artifact_entries: Optional[List[Dict[str, Any]]] = None,
    artifact_failure: bool = False,
    raw_kb_search_result: Optional[Dict[str, Any]] = None,
    fallback_message: str = ARTIFACT_FALLBACK_MESSAGE,
) -> str:
    main_answer = fallback_message if artifact_failure else (answer or "[No answer generated]")
    combined_sources = dedupe_documents((source_documents or []) + (corroborating_documents or []))
    sources_text = format_sources(combined_sources)
    return f"**Answer**:\n{main_answer}\n\n**Sources**:\n{sources_text}"


def _run_filtered_retrieval_qa(
    llm_model,
    vector_store,
    retriever,
    query: str,
    working_directory: Optional[str] = None,
    pdf_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run QA only on non-artifact retrieval docs and preserve artifact snippets for debug output.
    """
    retrieved_docs = get_relevant_documents(retriever, query)
    usable_docs, artifact_entries = split_artifact_documents(retrieved_docs, query=query)
    usable_docs = select_documents_for_qa(usable_docs, query=query, max_docs=8)

    print(
        f"Retrieved {len(retrieved_docs)} document(s); "
        f"filtered {len(artifact_entries)} artifact doc(s); "
        f"using {len(usable_docs)} doc(s)."
    )

    def _run_jsonl_fallback(reason: str) -> Dict[str, Any]:
        print(f"Running low-latency fallback reroute: {reason}")
        search_result = None
        if working_directory:
            search_result = direct_jsonl_kb_search(
                working_directory=working_directory,
                query=query,
                pdf_name=pdf_name,
                max_hits=12,
            )

        routing = select_primary_routed_source(search_result, query)
        primary_source = routing.get("primary_source")
        if primary_source:
            retry_k = 6 if query_prefers_visual_search(query) else 8
            retry_retriever = build_vector_store_retriever(
                vector_store,
                k=retry_k,
                metadata_filter={"source": primary_source},
            )
            retry_docs = get_relevant_documents(retry_retriever, query)
            retry_usable_docs, retry_artifact_entries = split_artifact_documents(retry_docs, query=query)
            retry_usable_docs = select_documents_for_qa(retry_usable_docs, query=query, max_docs=retry_k)
            fallback_docs = retry_usable_docs or select_routed_source_documents(
                search_result,
                primary_source,
                query,
                max_docs=retry_k,
            )

            if fallback_docs:
                print(
                    f"Primary-source fallback selected {primary_source} "
                    f"with {len(fallback_docs)} document(s)."
                )
                qa_chain = RetrievalQAWithSourcesChain.from_chain_type(
                    llm=llm_model,
                    chain_type="stuff",
                    retriever=build_static_retriever(fallback_docs),
                    return_source_documents=True,
                )
                result = qa_chain.invoke({"question": query})
                answer = result.get("answer", "[No answer generated]")
                source_documents = select_documents_for_qa(
                    result.get("source_documents", fallback_docs),
                    query=query,
                    max_docs=retry_k,
                )
                combined_artifact_entries = artifact_entries + retry_artifact_entries
                artifact_failure = answer_is_artifact_dominated(answer)
                non_answer_failure = answer_is_non_answer(answer)
                fallback_failed = artifact_failure or non_answer_failure
                corroborating_documents = []
                if not fallback_failed:
                    corroborating_documents = select_corroborating_documents(
                        search_result,
                        primary_source=primary_source,
                        answer=answer,
                        query=query,
                        max_sources=2,
                        max_docs_per_source=1,
                    )

                response_markdown = build_retrieval_response(
                    answer=answer,
                    source_documents=source_documents,
                    corroborating_documents=corroborating_documents,
                    artifact_entries=combined_artifact_entries,
                    artifact_failure=fallback_failed,
                    raw_kb_search_result=search_result if fallback_failed else None,
                )
                return {
                    "answer": ARTIFACT_FALLBACK_MESSAGE if fallback_failed else answer,
                    "sources_text": format_sources(source_documents + corroborating_documents),
                    "response_markdown": response_markdown,
                    "artifact_entries": combined_artifact_entries,
                    "artifact_failure": fallback_failed,
                    "source_documents": source_documents + corroborating_documents,
                    "raw_kb_search_result": search_result if fallback_failed else None,
                }

        fallback_docs = (search_result or {}).get("documents", [])
        if fallback_docs:
            print(f"Direct JSONL KB fallback found {len(fallback_docs)} usable document(s), but no clear primary source.")
            qa_chain = RetrievalQAWithSourcesChain.from_chain_type(
                llm=llm_model,
                chain_type="stuff",
                retriever=build_static_retriever(select_documents_for_qa(fallback_docs, query=query, max_docs=8)),
                return_source_documents=True,
            )
            result = qa_chain.invoke({"question": query})
            answer = result.get("answer", "[No answer generated]")
            source_documents = result.get("source_documents", fallback_docs)
            artifact_failure = answer_is_artifact_dominated(answer) or answer_is_non_answer(answer)
            response_markdown = build_retrieval_response(
                answer=answer,
                source_documents=source_documents,
                artifact_entries=artifact_entries,
                artifact_failure=artifact_failure,
                raw_kb_search_result=search_result,
            )
            return {
                "answer": ARTIFACT_FALLBACK_MESSAGE if artifact_failure else answer,
                "sources_text": format_sources(source_documents),
                "response_markdown": response_markdown,
                "artifact_entries": artifact_entries,
                "artifact_failure": artifact_failure,
                "source_documents": source_documents,
                "raw_kb_search_result": search_result,
            }

        response_markdown = build_retrieval_response(
            answer=ARTIFACT_FALLBACK_MESSAGE,
            source_documents=[],
            artifact_entries=artifact_entries,
            artifact_failure=bool(artifact_entries),
            raw_kb_search_result=search_result,
        )
        return {
            "answer": ARTIFACT_FALLBACK_MESSAGE,
            "sources_text": format_sources([]),
            "response_markdown": response_markdown,
            "artifact_entries": artifact_entries,
            "artifact_failure": bool(artifact_entries),
            "source_documents": [],
            "raw_kb_search_result": search_result,
        }

    if not usable_docs:
        return _run_jsonl_fallback("no usable first-pass documents")

    print("Initializing RetrievalQAWithSourcesChain with chain_type='stuff'...")
    qa_chain = RetrievalQAWithSourcesChain.from_chain_type(
        llm=llm_model,
        chain_type="stuff",
        retriever=build_static_retriever(usable_docs),
        return_source_documents=True,
    )

    print(f"Invoking QA chain with query: '{query}'")
    result = qa_chain.invoke({"question": query})

    answer = result.get("answer", "[No answer generated]")
    source_documents = select_documents_for_qa(
        result.get("source_documents", usable_docs),
        query=query,
        max_docs=8,
    )
    artifact_failure = answer_is_artifact_dominated(answer)
    non_answer_failure = answer_is_non_answer(answer)
    source_ambiguity = answer_has_ambiguous_sources(
        source_documents,
        query=query,
        explicit_pdf_name=pdf_name,
    )

    if artifact_failure:
        print("Generated answer matched known artifact pattern; trying direct JSONL KB fallback.")
        return _run_jsonl_fallback("artifact-dominated first-pass answer")
    if non_answer_failure:
        print("Generated answer was a non-answer; trying direct JSONL KB fallback.")
        return _run_jsonl_fallback("non-answer first-pass response")
    if source_ambiguity:
        print("First-pass answer used ambiguous mixed sources for a visual query; rerouting.")
        return _run_jsonl_fallback("mixed-source visual answer")

    response_markdown = build_retrieval_response(
        answer=answer,
        source_documents=source_documents,
        artifact_entries=artifact_entries,
        artifact_failure=artifact_failure,
    )

    return {
        "answer": ARTIFACT_FALLBACK_MESSAGE if artifact_failure else answer,
        "sources_text": format_sources(source_documents),
        "response_markdown": response_markdown,
        "artifact_entries": artifact_entries,
        "artifact_failure": artifact_failure,
        "source_documents": source_documents,
        "raw_kb_search_result": None,
    }
    
# ==============================================================================================================
#  Knowledge Base Sanitizer/Cleaner
# ==============================================================================================================
import json
import shutil
from pathlib import Path
from typing import Iterable, Set, Dict, Any, Optional


def knowledge_base_sanitizer(
    working_directory: str,
    pdfs_to_remove: Set[str],
    backup_suffix: str = "_backup_before_cleanup",
    recursive: bool = False,
    only_kb_filenames: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Clean ALL JSONL knowledge-base files in a directory by removing records whose
    'source' field matches any PDF in pdfs_to_remove.

    - Automatically finds `.jsonl` files in `working_directory` (optionally recursive).
    - Creates ONE backup per JSONL file (once) using `backup_suffix`.
    - Rewrites the original JSONL file in-place (no `_cleaned` output file).
    - Returns a structured summary.

    Parameters
    ----------
    working_directory : str
        Folder that contains KB `.jsonl` files (e.g., ...\\MyPapers).
    pdfs_to_remove : set[str]
        PDF basenames to remove (e.g., {"1985035.pdf", "contract.pdf"}).
    backup_suffix : str
        Suffix used for per-file backup copies (created once).
    recursive : bool
        If True, scans subdirectories too. If False, only top-level.
    only_kb_filenames : set[str] | None
        If provided, ONLY clean JSONL files whose filename is in this set.
        Example: {"01_chunks_kb.jsonl","02_visuals_kb.jsonl","03_metadata_kb.jsonl"}.

    Returns
    -------
    dict
        Summary with per-file stats.
    """
    workdir = Path(working_directory)
    if not workdir.exists():
        raise FileNotFoundError(f"Working directory not found: {workdir}")
    if not workdir.is_dir():
        raise NotADirectoryError(f"Not a directory: {workdir}")

    # Normalize PDFs-to-remove to basenames (case-insensitive match)
    remove_set = {Path(p).name.lower() for p in pdfs_to_remove if str(p).strip()}
    if not remove_set:
        raise ValueError("pdfs_to_remove is empty after normalization.")

    if only_kb_filenames is None:
        # Sensible default: only touch the KB files your pipeline writes
        only_kb_filenames = {
            "01_chunks_kb.jsonl",
            "02_visuals_kb.jsonl",
            "03_metadata_kb.jsonl",
        }

    jsonl_files = (
        sorted(workdir.rglob("*.jsonl")) if recursive else sorted(workdir.glob("*.jsonl"))
    )

    # Optionally restrict to specific KB filenames
    if only_kb_filenames:
        allowed = {n.lower() for n in only_kb_filenames}
        jsonl_files = [p for p in jsonl_files if p.name.lower() in allowed]

    if not jsonl_files:
        return {
            "working_directory": str(workdir),
            "files_processed": 0,
            "message": "No matching JSONL KB files found to clean.",
            "per_file": [],
        }

    per_file = []
    total_removed = 0
    total_kept = 0
    total_malformed = 0

    for jsonl_path in jsonl_files:
        backup_path = jsonl_path.with_name(f"{jsonl_path.stem}{backup_suffix}{jsonl_path.suffix}")
        tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")

        # Backup once
        backup_created = False
        if not backup_path.exists():
            shutil.copy(jsonl_path, backup_path)
            backup_created = True

        removed_count = 0
        kept_count = 0
        malformed_count = 0

        with jsonl_path.open("r", encoding="utf-8") as infile, tmp_path.open("w", encoding="utf-8") as outfile:
            for line_number, line in enumerate(infile, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    malformed_count += 1
                    continue

                # Robust source extraction:
                # - handles basename vs full path
                # - handles missing/None
                source_val = record.get("source")
                source_base = Path(str(source_val)).name.lower() if source_val is not None else ""

                if source_base in remove_set:
                    removed_count += 1
                    continue

                outfile.write(json.dumps(record, ensure_ascii=False) + "\n")
                kept_count += 1

        # Replace original in-place (atomic-ish on Windows via move)
        shutil.move(str(tmp_path), str(jsonl_path))

        per_file.append(
            {
                "file": str(jsonl_path),
                "backup": str(backup_path),
                "backup_created": backup_created,
                "removed": removed_count,
                "kept": kept_count,
                "malformed": malformed_count,
            }
        )

        total_removed += removed_count
        total_kept += kept_count
        total_malformed += malformed_count

    return {
        "working_directory": str(workdir),
        "files_processed": len(per_file),
        "pdfs_removed": sorted(remove_set),
        "totals": {
            "removed": total_removed,
            "kept": total_kept,
            "malformed": total_malformed,
        },
        "per_file": per_file,
    }


# # =========================
# # Example usage
# # =========================
# if __name__ == "__main__":
#     PDFS_TO_REMOVE = {
#         "RADIANT_LLM.pdf",
#         "v001t01a003-vvuq2025-152220.pdf",
#     }

#     summary = knowledge_base_sanitizer(
#         working_directory=r"C:...\clean_kbs",
#         pdfs_to_remove=PDFS_TO_REMOVE,
#         backup_suffix="_backup_before_cleanup",
#         recursive=False,  # set True if KBs are in subfolders
#         only_kb_filenames={"01_chunks_kb.jsonl", "02_visuals_kb.jsonl", "03_metadata_kb.jsonl"},
#     )

#     print(json.dumps(summary, indent=2))

# ===========================================================================
# Shared PDF pipeline helpers (used by Nougat, Lightweight, and router)
# ===========================================================================
# Callback bundle for embedding/retrieval; set by pdf_tools.PDFAnalyser before calling EmbeddingOnlyUtility/RetrievalOnlyUtility.
cb = None  # type: Any

def append_to_jsonl(jsonl_file: str, new_data: List[Dict]) -> None:
    """Append new_data (list of dicts) to a JSONL file."""
    try:
        dir_name = os.path.dirname(jsonl_file)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        if not isinstance(new_data, list):
            return
        with open(jsonl_file, "a", encoding="utf-8") as f:
            for row in new_data:
                if isinstance(row, dict):
                    try:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        print(f"Error appending to JSONL {jsonl_file}: {e}")

def make_document_id(source: str) -> str:
    import hashlib
    try:
        return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return source

def load_processed_pdfs(processed_file: str) -> List[str]:
    if not os.path.exists(processed_file):
        return []
    try:
        with open(processed_file, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except UnicodeDecodeError:
        try:
            with open(processed_file, "r", encoding="latin-1") as f:
                return f.read().splitlines()
        except Exception:
            return []
    return []

def save_processed_pdfs(processed_file: str, processed_pdfs: List[str]) -> None:
    def sanitize(s: str) -> str:
        return s.encode("utf-8", errors="replace").decode("utf-8")
    with open(processed_file, "w", encoding="utf-8") as f:
        for p in processed_pdfs:
            f.write(sanitize(p) + "\n")

# Nougat model/processor/device: load once and reuse.
# Keep this lazy so importing the module does not force model download/loading.
_nougat_processor, _nougat_model, _nougat_device = None, None, None

def get_nougat_bundle():
    global _nougat_processor, _nougat_model, _nougat_device
    if _nougat_processor is None:
        _nougat_processor, _nougat_model, _nougat_device = NougatInitializer()
    return _nougat_processor, _nougat_model, _nougat_device

def looks_scanned_or_low_text(pdf_path: str, pages_to_check: int = 3, min_chars: int = 2000) -> bool:
    """True if PDF appears scanned or has very little extractable text."""
    try:
        doc = fitz.open(pdf_path)
        n = min(len(doc), max(1, pages_to_check))
        total = 0
        for i in range(n):
            total += len((doc.load_page(i).get_text("text") or "").strip())
        doc.close()
        return total < min_chars
    except Exception:
        return True

# ===========================================================================
# EmbeddingOnlyUtility and RetrievalOnlyUtility (use module-level cb)
# ===========================================================================

def EmbeddingOnlyUtility(
    working_directory: str,
    # index_file: str = 'documents_library.json',  # kept for backward compatibility
    rebuild: bool = False
) -> Union[bool, str]:
    """
    Loads pre-chunked document content from JSONL KBs (chunks + figures),
    creates vector embeddings, and persists the vector store.

    Args:
        working_directory (str): Directory containing KB files and vector store.
        index_file (str, optional): Legacy parameter (not used for JSONL flow).
        rebuild (bool, optional): If True, deletes existing vector store before rebuild.
    """

    # ----------------------------
    # Validate embedding model
    # ----------------------------
    embedding_model = cb.embedding_model if hasattr(cb, 'embedding_model') else None
    persistent_dir_name = cb.persistent_dir if hasattr(cb, 'persistent_dir') else 'default_vector_store'

    if not embedding_model:
        return "Error: Embedding model is not initialized. Please set `cb.embedding_model`."

    # ----------------------------
    # Helper: Safe JSONL reader
    # ----------------------------
    def read_jsonl(jsonl_path: str) -> List[Dict]:
        rows = []
        try:
            if not os.path.exists(jsonl_path):
                print(f"Warning: JSONL file not found: {jsonl_path}")
                return rows

            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        print(
                            f"Warning: Skipping corrupted JSONL line {line_num} "
                            f"in {jsonl_path}: {e}"
                        )
        except Exception as e:
            print(f"Error reading JSONL file {jsonl_path}: {e}")
        return rows

    # ----------------------------
    # Load KBs
    # ----------------------------
    chunks_path = os.path.join(working_directory, "01_chunks_kb.jsonl")
    figures_path = os.path.join(working_directory, "02_visuals_kb.jsonl")

    chunks_data = read_jsonl(chunks_path)
    figures_data = read_jsonl(figures_path)

    if not chunks_data and not figures_data:
        return "No content found for embedding. Both 01_chunks_kb.jsonl and 02_visuals_kb.jsonl are empty or missing."

    # ----------------------------
    # Build LangChain Documents
    # ----------------------------
    documents = []

    # ---- Embed text chunks ----
    for doc in chunks_data:
        try:
            content = (doc.get("content") or "").strip()
            if not content:
                continue

            documents.append(
                Document(
                    page_content=content,
                    metadata={
                        "source": doc.get("source"),
                        "page": doc.get("page"),
                        "chunk_index": doc.get("chunk_index"),
                        "document_id": doc.get("document_id"),
                        "chunk_id": doc.get("chunk_id"),
                        "doc_type": "text",
                    }
                )
            )
        except Exception as e:
            print(f"Warning: Failed to prepare text chunk for embedding: {e}")

    # ---- Embed figure descriptions ----
    for fig in figures_data:
        try:
            desc = (fig.get("description") or "").strip()
            if not desc:
                continue

            documents.append(
                Document(
                    page_content=desc,
                    metadata={
                        "source": fig.get("source"),
                        "page": fig.get("page"),
                        "figure_index": fig.get("figure_index"),
                        "document_id": fig.get("document_id"),
                        "figure_id": fig.get("figure_id"),
                        "doc_type": "figure",
                    }
                )
            )
        except Exception as e:
            print(f"Warning: Failed to prepare figure description for embedding: {e}")

    if not documents:
        return "No valid documents found for embedding after preprocessing."

    # ----------------------------
    # Vector store setup
    # ----------------------------
    vector_store_dir = os.path.join(working_directory, persistent_dir_name)

    # ---- Rebuild logic ----
    if rebuild:
        if os.path.exists(vector_store_dir):
            print(f"Rebuild requested: Deleting existing vector store at {vector_store_dir}...")
            try:
                import shutil
                shutil.rmtree(vector_store_dir)
                print("Existing vector store deleted.")
            except Exception as e:
                return f"Error deleting existing vector store for rebuild: {e}"

    os.makedirs(vector_store_dir, exist_ok=True)

    # ----------------------------
    # Create / load Chroma
    # ----------------------------
    try:
        if os.path.exists(vector_store_dir) and os.listdir(vector_store_dir) and not rebuild:
            print(f"Loading existing Chroma vector store from {vector_store_dir}...")
            vector_store = Chroma(
                persist_directory=vector_store_dir,
                embedding_function=embedding_model
            )
        else:
            print(f"Creating new Chroma vector store at {vector_store_dir}...")
            vector_store = Chroma(
                embedding_function=embedding_model,
                persist_directory=vector_store_dir
            )

        # ----------------------------
        # Batched embedding
        # ----------------------------
        BATCH_SIZE = 100
        total_docs = len(documents)

        print(f"Starting embedding process for {total_docs} documents (batch size = {BATCH_SIZE})...")

        for i in range(0, total_docs, BATCH_SIZE):
            batch = documents[i:i + BATCH_SIZE]
            try:
                print(
                    f"Embedding batch {i // BATCH_SIZE + 1}/"
                    f"{(total_docs + BATCH_SIZE - 1) // BATCH_SIZE} "
                    f"({len(batch)} documents)"
                )
                vector_store.add_documents(batch)
            except Exception as e:
                print(f"Warning: Failed to embed batch starting at index {i}: {e}")

        # vector_store.persist()   # NOT Needed in recent ChromaDB version. Done automatically
        print("Chroma vector store created/updated and persisted successfully.")
        return True

    except Exception as e:
        print(f"Exception during embedding: {e}")
        import traceback
        traceback.print_exc()
        return f"Exception during embedding: {e}"

#--------------------End of Embedding Logic--------------------------------------

#==================================================================================================
#  Retrieval Utility: Retrieval Only
#==================================================================================================

def RetrievalOnlyUtility(
    query: str,
    working_directory: str,
    pdf_name: Optional[str] = None,
) -> str:
    """
    Loads the persistent vector store and runs retrieval QA over embedded documents.
    Also Rebuilds the vector store from the Json database the if the vector store is missing
    """
    
    # Ensure cb (common_bundle) is accessible, or pass models directly
    llm_model = cb.llm_model if hasattr(cb, 'llm_model') else None
    embedding_model = cb.embedding_model if hasattr(cb, 'embedding_model') else None
    persistent_dir = cb.persistent_dir if hasattr(cb, 'persistent_dir') else 'default_vector_store'

    if not embedding_model or not llm_model:
        return "Error: Required models not initialized."

    vector_store_dir = os.path.join(working_directory, persistent_dir)
    # If the store is missing or empty, build it now:
    if not os.path.exists(vector_store_dir) or not os.listdir(vector_store_dir):
        print("Vector store not found or empty inside RetrievalOnlyUtility — rebuilding...")
        rebuild_result = EmbeddingOnlyUtility(
            working_directory=working_directory,
            # index_file="documents_library.json",
            rebuild=True
        )
        if rebuild_result is not True:
            return f"Error rebuilding vector store: {rebuild_result}"
        print("Vector store rebuild successful; proceeding with retrieval.")

    try:
        print(f"Loading Chroma vector store from {vector_store_dir}...")
        # Load the existing vector store, DO NOT recreate it
        vector_store = Chroma(persist_directory=vector_store_dir, embedding_function=embedding_model)
        print("Chroma vector store loaded successfully.")
        
        requested_sources = infer_pdf_sources_from_query(query, pdf_name, working_directory)
        metadata_filter = None
        if requested_sources:
            if len(requested_sources) == 1:
                metadata_filter = {"source": requested_sources[0]}
            else:
                metadata_filter = {"source": {"$in": requested_sources}}
            print(f"Applying source filter to retrieval: {requested_sources}")

        # Configure the retriever with best-effort metadata filtering:
        retriever = build_vector_store_retriever(vector_store, k=20, metadata_filter=metadata_filter)
        retrieval_result = _run_filtered_retrieval_qa(
            llm_model,
            vector_store,
            retriever,
            query,
            working_directory=working_directory,
            pdf_name=pdf_name,
        )
        return retrieval_result["response_markdown"]
    except Exception as e:
        # Catch specific LangChain errors if possible, or log full traceback
        print(f"An error occurred during retrieval: {e}")
        return f"Retrieval failed: {str(e)}. Please check logs for details."
    
# ============================================================================
# NougatPDFProcessorUtility
# ============================================================================
def NougatPDFProcessorUtility(
    working_directory: str,
    only_process_these: List[str],
    alert_sink: Optional[List] = None,
    nougat_dpi: int = 200,
) -> Tuple[str, List[str]]:
    """
    Processes new PDFs with Nougat OCR, chunks text, writes to 01_chunks_kb.jsonl.
    Uses shared helpers and lazy Nougat bundle. Embeddings handled separately.
    """
    if not os.path.exists(working_directory) or not os.path.isdir(working_directory):
        return f"Error: Directory '{working_directory}' does not exist or is not a directory.", []

    print("Extracting text with NougatPDFProcessorUtility...")
    log_file = os.path.join(working_directory, "05_pdf_processing.log")
    logging.basicConfig(filename=log_file, level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")

    processor, model, device = get_nougat_bundle()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=100,
        length_function=len,
        separators=["\n\n", "\n", " ", ""],
    )

    def process_pdf_file(pdf_path: str) -> List[Dict]:
            extracted_chunks = []
            pdf_name = os.path.basename(pdf_path)

            try:
                # Use a higher raster DPI for scanned/low-quality pages.
                images = RasterizePaper(pdf=pdf_path, return_pil=True, dpi=nougat_dpi)
                if not images:
                    print(f"No images rasterized for {pdf_name}")
                    return []

                non_empty_pages = 0
                for page_num, image_bytes_obj in enumerate(images):  # BytesIO per page
                    image = Image.open(io.BytesIO(image_bytes_obj.getvalue())).convert("RGB")
                    pixel_values = processor(images=image, return_tensors="pt").pixel_values.to(device)
                    unk_id = getattr(processor.tokenizer, "unk_token_id", None)
                    gen_kw = dict(
                        min_length=1,
                        max_length=3584,
                        return_dict_in_generate=True,
                        output_scores=True,
                        stopping_criteria=StoppingCriteriaList([StoppingCriteriaScores()]),
                    )
                    if unk_id is not None:
                        gen_kw["bad_words_ids"] = [[unk_id]]
                    outputs = model.generate(pixel_values, **gen_kw)

                    generated_text = processor.batch_decode(outputs[0], skip_special_tokens=True)[0]
                    generated_text = processor.post_process_generation(generated_text, fix_markdown=False)
                    clean_text = (generated_text or "").strip()
                    if not clean_text:
                        print(f"Nougat: page {page_num + 1} produced no text for {pdf_name}")
                    else:
                        non_empty_pages += 1
                        print(f"Nougat: page {page_num + 1} text length for {pdf_name}: {len(clean_text)} chars")

                    # --- CHUNK THE EXTRACTED TEXT HERE ---
                    chunks_from_page = text_splitter.split_text(generated_text or "")
                    
                    #----------------------------------
                    document_id = make_document_id(pdf_name)
                    for i, chunk_content in enumerate(chunks_from_page):
                        try:
                            extracted_chunks.append({
                                "source": pdf_name,
                                "page": page_num + 1,
                                "content": chunk_content,
                                "chunk_index": i,
                                "document_id": document_id,
                                "chunk_id": f"{document_id}:p{page_num+1}:c{i}",
                            })
                        except Exception as e:
                            print(f"Warning: Failed to create chunk record (page {page_num+1}, chunk {i}): {e}")

                    
                    #-----------------------------------
                    
                    #for i, chunk_content in enumerate(chunks_from_page):
                    #    extracted_chunks.append({
                    #        "source": pdf_name,
                    #        "page": page_num + 1,
                    #        "content": chunk_content,
                    #        "chunk_index": i, # Add a chunk index for more granular identification
                    #        # Metadata for figures and general PDF info will be merged later in describe_figures_for_new_pdfs
                    #    })
                    #-----------------------------------

            except Exception as e:
                logging.error(f"Error processing {pdf_name}: {e}")
                return []

                print(
                    f"Nougat summary for {pdf_name}: {non_empty_pages}/{len(images)} pages yielded non-empty text; "
                    f"{len(extracted_chunks)} chunks created."
                )
            return extracted_chunks

    processed_file_path = os.path.join(working_directory, "04_processed_pdfs.txt")
    processed_pdfs_list = load_processed_pdfs(processed_file_path)
    pdfs_to_process = [p for p in only_process_these if os.path.basename(p) not in processed_pdfs_list]

    if not pdfs_to_process:
        print("No new PDFs to process by Nougat.")
        return "No new PDFs to process by Nougat.", []

    extracted_data_from_new_pdfs = []
    newly_processed_pdf_basenames = []

    # Process PDFs sequentially to avoid Hugging Face model thread-safety issues
    for pdf_path in pdfs_to_process:
        try:
            result_chunks = process_pdf_file(pdf_path)
            extracted_data_from_new_pdfs.extend(result_chunks)
            # Only mark as processed if we got at least one chunk (so 0-chunk PDFs are retried later)
            if result_chunks:
                newly_processed_pdf_basenames.append(os.path.basename(pdf_path))
            else:
                pdf_name = os.path.basename(pdf_path)
                warning_msg = f"Nougat produced 0 chunks for '{pdf_name}'. It will be retried on the next run."
                print(warning_msg)
                if alert_sink is not None:
                    try:
                        alert_sink.append(dbc.Alert(warning_msg, color="warning", dismissable=True))
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Error in processing {pdf_path}: {e}")
            if alert_sink is not None:
                try:
                    alert_sink.append(
                        dbc.Alert(
                            f"Nougat failed for '{os.path.basename(pdf_path)}': {e}",
                            color="danger",
                            dismissable=True,
                        )
                    )
                except Exception:
                    pass

    chunks_path = os.path.join(working_directory, "01_chunks_kb.jsonl")
    try:
        append_to_jsonl(chunks_path, extracted_data_from_new_pdfs)
    except Exception as e:
        print(f"Error writing chunks KB: {e}")

    current_processed = set(load_processed_pdfs(processed_file_path))
    current_processed.update(newly_processed_pdf_basenames)
    save_processed_pdfs(processed_file_path, list(current_processed))

    return (
        f"PDF content extraction and chunking complete. {len(newly_processed_pdf_basenames)} "
        f"files processed, resulting in {len(extracted_data_from_new_pdfs)} chunks."
    ), newly_processed_pdf_basenames


# ============================================================================
# LightweightPDFProcessorUtility (PyMuPDF only, writes 01_chunks_kb.jsonl)
# ============================================================================
def LightweightPDFProcessorUtility(
    working_directory: str,
    only_process_these: List[str],
) -> Tuple[str, List[str]]:
    """Extract text with PyMuPDF, chunk, write to 01_chunks_kb.jsonl. No Nougat."""
    if not os.path.exists(working_directory) or not os.path.isdir(working_directory):
        return f"Error: Directory '{working_directory}' does not exist or is not a directory.", []

    print("Extracting text with LightweightPDFProcessorUtility...")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=200,
        length_function=len,
        separators=["\n\n", "\n", " ", ""],
    )
    processed_file_path = os.path.join(working_directory, "04_processed_pdfs.txt")
    processed_pdfs_list = load_processed_pdfs(processed_file_path)
    pdfs_to_process = [p for p in only_process_these if os.path.basename(p) not in processed_pdfs_list]

    if not pdfs_to_process:
        return "No new PDFs to process by lightweight extractor.", []

    all_chunks = []
    newly_processed_basenames = []

    for pdf_path in pdfs_to_process:
        pdf_name = os.path.basename(pdf_path)
        per_pdf_chunk_count = 0
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                raw_text = (doc.load_page(page_num).get_text("text") or "").strip()
                if not raw_text:
                    continue
                chunks_from_page = text_splitter.split_text(raw_text)
                doc_id = make_document_id(pdf_name)
                for i, content in enumerate(chunks_from_page):
                    all_chunks.append({
                        "source": pdf_name,
                        "page": page_num + 1,
                        "content": content,
                        "chunk_index": i,
                        "document_id": doc_id,
                        "chunk_id": f"{doc_id}:p{page_num+1}:c{i}",
                    })
                    per_pdf_chunk_count += 1
            doc.close()
            # Mark as processed only if this PDF yielded at least one chunk.
            if per_pdf_chunk_count > 0:
                newly_processed_basenames.append(pdf_name)
            else:
                print(f"Lightweight extractor produced 0 chunks for '{pdf_name}'. It will be retried/fallbacked.")
        except Exception as e:
            logging.error(f"Lightweight extraction failed for {pdf_name}: {e}")

    if all_chunks:
        append_to_jsonl(os.path.join(working_directory, "01_chunks_kb.jsonl"), all_chunks)
    if newly_processed_basenames:
        current = set(load_processed_pdfs(processed_file_path))
        current.update(newly_processed_basenames)
        save_processed_pdfs(processed_file_path, list(current))

    return (
        f"Lightweight extraction complete. {len(newly_processed_basenames)} files, {len(all_chunks)} chunks."
    ), newly_processed_basenames


# ============================================================================
# Router: choose lightweight vs Nougat (with optional fallback)
# ============================================================================
def run_pdf_text_extraction_router(
    working_directory: str,
    new_pdfs: List[str],
    text_mode: str = "nougat",
    global_external_alerts: Optional[List] = None,
    min_chars_for_lightweight: int = 2000,
) -> Tuple[str, List[str]]:
    """
    Routes text extraction: either Nougat only, or lightweight with fallback to Nougat for scanned/low-text PDFs.
    Returns (summary_message, list of basenames of successfully processed PDFs).
    """
    effective = text_mode.lower() if text_mode else "nougat"
    if effective == "nougat":
        return NougatPDFProcessorUtility(
            working_directory=working_directory,
            only_process_these=new_pdfs,
            alert_sink=global_external_alerts,
        )

    # lightweight (with per-PDF fallback to Nougat)
    pdfs_light = []
    pdfs_nougat = []
    for p in new_pdfs:
        if looks_scanned_or_low_text(p, min_chars=min_chars_for_lightweight):
            pdfs_nougat.append(p)
        else:
            pdfs_light.append(p)

    if pdfs_nougat and global_external_alerts is not None:
        try:
            global_external_alerts.append(
                dbc.Alert(
                    f"🧾 {len(pdfs_nougat)} PDF(s) detected as scanned/low-text. Using high-fidelity OCR.",
                    color="warning",
                    dismissable=True,
                )
            )
        except Exception:
            pass

    summary_parts = []
    all_basenames = []

    if pdfs_light:
        s, b = LightweightPDFProcessorUtility(working_directory=working_directory, only_process_these=pdfs_light)
        summary_parts.append(s)
        all_basenames.extend(b)
        # Immediate fallback: files sent to lightweight but yielding 0 chunks go to Nougat now.
        lightweight_success = set(b)
        pdfs_light_fallback = [p for p in pdfs_light if os.path.basename(p) not in lightweight_success]
        if pdfs_light_fallback:
            if global_external_alerts is not None:
                try:
                    global_external_alerts.append(
                        dbc.Alert(
                            f"{len(pdfs_light_fallback)} PDF(s) yielded 0 lightweight chunks. Retrying with Nougat.",
                            color="warning",
                            dismissable=True,
                        )
                    )
                except Exception:
                    pass
            pdfs_nougat.extend(pdfs_light_fallback)
    if pdfs_nougat:
        # De-duplicate full paths while preserving order.
        dedup_nougat = list(dict.fromkeys(pdfs_nougat))
        s, b = NougatPDFProcessorUtility(
            working_directory=working_directory,
            only_process_these=dedup_nougat,
            alert_sink=global_external_alerts,
        )
        summary_parts.append(s)
        all_basenames.extend(b)

    return " ".join(summary_parts) if summary_parts else "No text extraction performed.", all_basenames


# ---------------------------------------------------------------------------
# Extraction-only utilities: LightweightPDFProcessorUtility, NougatPDFProcessorUtility,
# and run_pdf_text_extraction_router (above). All routing and full pipeline
# (figures, metadata, embed, retrieve) are in tools.pdf_tools.PDFAnalyser only.
# radiant_llm calls PDFAnalyser via PDFReaderTool.
# ---------------------------------------------------------------------------
