"""
Minimal PDF tooling bridge for RADIANT-LLM.

This module intentionally keeps only the active PDF analysis path used by
`radiant_llm.py`:
- `VisualParserPDFAnalyser`

The implementation delegates parsing to the modular Visual-Parser pipeline in
`utils/vp_*`, then runs embedding + retrieval via shared helpers.
"""

from __future__ import annotations

import os
from typing import List, Optional

import dash_bootstrap_components as dbc
from dotenv import load_dotenv

from utils.vp_config import ParserConfig as VPParserConfig
from utils.vp_pdf_tracker import find_new_pdfs
from utils.vp_pipeline import run_pipeline as run_vp_pipeline
import utils.pdf_helpers as shared_pdf_helpers


load_dotenv()

openai_key = os.getenv("OPENAI_API_KEY", "")
gemini_key = os.getenv("GEMINI_API_KEY", "")


def VisualParserPDFAnalyser(
    query: str,
    working_directory: str,
    cb=None,
    global_external_alerts: Optional[List] = None,
    rebuild_vector_store: bool = False,
    pdf_name: Optional[str] = None,
    text_mode: str = "nougat",
    vision_detail: str = "low",
    metadata_pages: int = 2,
    max_workers: int = 1,
    gpt_reasoning_effort: str = "medium",
    log_level: str = "INFO",
) -> str:
    """
    Parse PDFs with the Visual-Parser pipeline, then run embedding + retrieval.

    Args:
        query: User question for retrieval.
        working_directory: Directory containing PDFs and parser outputs.
        cb: Chatbot context object (must provide llm/embedding handles).
        global_external_alerts: Optional Dash alert sink.
        rebuild_vector_store: Force full vector store rebuild.
        pdf_name: If provided, restrict retrieval/fallback routing to this PDF basename.
        text_mode: "nougat" or "lightweight" for text extraction.
        vision_detail: "low" | "high" | "auto".
        metadata_pages: Number of front pages used for metadata extraction.
        max_workers: Thread workers used during parsing.
        gpt_reasoning_effort: GPT reasoning effort for vision calls.
        log_level: Pipeline log level.
    """
    if cb is None:
        return "Error: Chatbot context (cb) is required."

    llm_model = getattr(cb, "llm_model", None)
    embedding_model = getattr(cb, "embedding_model", None)
    if llm_model is None or embedding_model is None:
        return "Error: Embedding model or LLM model is not initialized. Please initialize a model first."

    llm_type = (getattr(cb, "llm_type", "gpt") or "gpt").lower()
    if llm_type == "gemini":
        vision_provider = "gemini"
        vision_model = getattr(cb, "model_choice", None) or "gemini-2.5-flash"
    elif llm_type == "local":
        if openai_key:
            vision_provider = "gpt"
            vision_model = getattr(cb, "gpt_vision_llm", None) or "gpt-4o"
        elif gemini_key:
            vision_provider = "gemini"
            vision_model = "gemini-2.5-flash"
        else:
            vision_provider = "gpt"
            vision_model = "gpt-4o"
    else:
        vision_provider = "gpt"
        vision_model = (
            getattr(cb, "gpt_vision_llm", None)
            or getattr(cb, "model_choice", None)
            or "gpt-5.4"
        )

    vp_config = VPParserConfig(
        input_dir=working_directory,
        output_dir=working_directory,
        text_mode=(text_mode or "nougat"),
        vision_provider=vision_provider,
        openai_api_key=openai_key,
        gpt_vision_model=vision_model if vision_provider == "gpt" else "gpt-5.4",
        gpt_reasoning_effort=(gpt_reasoning_effort or "medium"),
        gemini_api_key=gemini_key,
        gemini_vision_model=vision_model if vision_provider == "gemini" else "gemini-2.5-flash",
        vision_detail=(vision_detail or "low"),
        metadata_pages=max(1, int(metadata_pages)),
        max_workers=max(1, int(max_workers)),
        rebuild=rebuild_vector_store,
        log_level=(log_level or "INFO"),
    )

    if global_external_alerts is not None:
        try:
            detected_count = len(
                find_new_pdfs(vp_config.input_dir, rebuild=vp_config.rebuild)
            )
            if detected_count > 0:
                global_external_alerts.append(
                    dbc.Alert(
                        f"📚 Visual-Parser: PDF(s) detected: ({detected_count}). Processing...",
                        color="info",
                        dismissable=True,
                    )
                )
            else:
                global_external_alerts.append(
                    dbc.Alert(
                        "📚 Visual-Parser: No new PDF(s) detected. Using existing database.",
                        color="info",
                        dismissable=True,
                    )
                )
        except Exception:
            pass

    try:
        summary = run_vp_pipeline(vp_config)
    except Exception as exc:
        return f"Error running Visual-Parser pipeline: {exc}"

    if global_external_alerts is not None:
        try:
            global_external_alerts.append(
                dbc.Alert(
                    "Visual-Parser: Processing complete.",
                    color="info",
                    dismissable=True,
                )
            )
        except Exception:
            pass

    shared_pdf_helpers.cb = cb

    if summary.get("processed_basenames") or rebuild_vector_store:
        embed_result = shared_pdf_helpers.EmbeddingOnlyUtility(
            working_directory=working_directory,
            rebuild=rebuild_vector_store,
        )
        if embed_result is not True:
            return f"Error during embedding: {embed_result}"

    if global_external_alerts is not None:
        try:
            global_external_alerts.append(
                dbc.Alert(
                    "🔍📚 Now querying the database...",
                    color="success",
                    dismissable=True,
                )
            )
        except Exception:
            pass

    retrieval_result = shared_pdf_helpers.RetrievalOnlyUtility(
        query=query,
        working_directory=working_directory,
        pdf_name=pdf_name,
    )
    return f"Visual-Parser-backed PDF analysis complete.\n\n{retrieval_result}"


__all__ = ["VisualParserPDFAnalyser"]
