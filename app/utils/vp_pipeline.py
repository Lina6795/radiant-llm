"""
pipeline.py Ã¢â‚¬â€ The main Visual-RAG parsing orchestrator.

Calls each stage in order:
    0.   Detect new PDFs
    0.5  Extract per-document metadata (Vision LLM on front pages)
    1.   Extract and chunk text  (Nougat  OR  Lightweight, controlled by config)
    2.   Describe figures        (Vision LLM, page-by-page)
    3.   Write metadata JSONL
    4.   Mark PDFs as processed

No vector store, no embeddings, no retrieval Ã¢â‚¬â€ pure JSONL generation.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from utils.vp_config import ParserConfig
from utils.vp_figure_describer import describe_figures_for_new_pdfs
from utils.vp_jsonl_writer import append_to_jsonl, make_document_id, read_jsonl
from utils.vp_metadata_extractor import extract_pdf_metadata
from utils.vp_pdf_tracker import (
    PROCESSED_REGISTRY,
    find_new_pdfs,
    load_processed_pdfs,
    mark_as_processed,
    save_processed_pdfs,
)

logger = logging.getLogger(__name__)


def _setup_logging(config: ParserConfig) -> None:
    log_level = getattr(logging, config.log_level.upper(), logging.ERROR)
    log_path  = os.path.join(config.effective_output_dir(), "05_pipeline.log")
    logging.basicConfig(
        filename=log_path,
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Also log to stdout so the CLI shows progress
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger().addHandler(console)


def _recover_stale_registry_entries(input_dir: str, output_dir: str, registry_path: str) -> int:
    """
    Remove processed-PDF registry entries for files that have no chunk records.
    This recovers from earlier failed runs that incorrectly marked PDFs as done.
    """
    if not os.path.exists(registry_path):
        return 0

    processed = load_processed_pdfs(registry_path)
    if not processed:
        return 0

    existing_basenames = {
        filename.lower()
        for root, _, files in os.walk(input_dir)
        for filename in files
        if filename.lower().endswith(".pdf")
    }
    if not existing_basenames:
        return 0

    chunks_path = os.path.join(output_dir, "01_chunks_kb.jsonl")
    visuals_path = os.path.join(output_dir, "02_visuals_kb.jsonl")
    extracted_sources = set()

    for row in read_jsonl(chunks_path):
        source = row.get("source")
        if isinstance(source, str) and source.lower().endswith(".pdf"):
            extracted_sources.add(source.lower())

    for row in read_jsonl(visuals_path):
        source = row.get("source")
        if isinstance(source, str) and source.lower().endswith(".pdf"):
            extracted_sources.add(source.lower())

    stale = [
        name for name in processed
        if name.lower() in existing_basenames and name.lower() not in extracted_sources
    ]
    if not stale:
        return 0

    cleaned = [name for name in processed if name not in stale]
    save_processed_pdfs(registry_path, sorted(set(cleaned)))
    logger.warning(
        "Recovered %d stale processed registry entries: %s",
        len(stale),
        ", ".join(sorted(stale)),
    )
    return len(stale)


def run_pipeline(config: Optional[ParserConfig] = None) -> Dict:
    """
    Execute the full Visual-RAG parsing pipeline.

    Args:
        config: A :class:`~visual_parser.config.ParserConfig` instance.
                When *None*, one is built from environment variables via
                :meth:`ParserConfig.from_env`.

    Returns:
        A summary dict::

            {
                "new_pdfs_found":       int,
                "text_chunks_written":  int,
                "figures_written":      int,
                "metadata_written":     int,
                "processed_basenames":  List[str],
            }
    """
    if config is None:
        config = ParserConfig.from_env()

    config.validate()
    output_dir = config.effective_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    _setup_logging(config)

    summary = {
        "new_pdfs_found":      0,
        "text_chunks_written": 0,
        "figures_written":     0,
        "metadata_written":    0,
        "processed_basenames": [],
    }

    # -----------------------------------------------------------------------
    # Step 0 Ã¢â‚¬â€ Discover new PDFs
    # -----------------------------------------------------------------------
    registry_path = os.path.join(output_dir, PROCESSED_REGISTRY)
    new_pdfs      = find_new_pdfs(config.input_dir, rebuild=config.rebuild)

    summary["new_pdfs_found"] = len(new_pdfs)

    if not new_pdfs and not config.rebuild:
        recovered = _recover_stale_registry_entries(config.input_dir, output_dir, registry_path)
        if recovered:
            new_pdfs = find_new_pdfs(config.input_dir, rebuild=False)
            summary["new_pdfs_found"] = len(new_pdfs)

    if not new_pdfs:
        print("No new PDFs found. Nothing to do.")
        return summary

    print(f"Found {len(new_pdfs)} new PDF(s). Starting pipeline Ã¢â‚¬Â¦")

    # -----------------------------------------------------------------------
    # Step 0.5 Ã¢â‚¬â€ Metadata extraction (Vision LLM on front pages)
    # -----------------------------------------------------------------------
    _vision_api_key = (
        config.openai_api_key if config.vision_provider == "gpt" else config.gemini_api_key
    )
    _vision_model = (
        config.gpt_vision_model if config.vision_provider == "gpt" else config.gemini_vision_model
    )

    pdf_meta_map: Dict[str, dict] = {}
    for pdf_path in new_pdfs:
        try:
            meta = extract_pdf_metadata(
                pdf_path              = pdf_path,
                vision_provider       = config.vision_provider,
                vision_api_key        = _vision_api_key,
                vision_model          = _vision_model,
                num_pages             = config.metadata_pages,
                vision_detail         = config.vision_detail,
                reasoning_effort      = config.gpt_reasoning_effort,
            )
            pdf_meta_map[pdf_path] = meta
        except Exception as exc:
            logger.warning("Metadata extraction failed for %s: %s", pdf_path, exc)
            pdf_meta_map[pdf_path] = {"_error": str(exc)}

    # -----------------------------------------------------------------------
    # Step 1 Ã¢â‚¬â€ Text extraction and chunking
    # -----------------------------------------------------------------------
    if config.text_mode == "nougat":
        print("[Step 1] Running Nougat text extraction ...")
        from utils.vp_nougat_engine import NougatInitializer
        from utils.vp_text_extractor import lightweight_extract_pdfs, nougat_extract_pdfs

        try:
            processor, model, device = NougatInitializer(config.nougat_model)
            nougat_summary, processed_basenames = nougat_extract_pdfs(
                only_process_these = new_pdfs,
                output_dir         = output_dir,
                processor          = processor,
                model              = model,
                device             = device,
                chunk_size         = config.chunk_size,
                chunk_overlap      = config.chunk_overlap,
                max_workers        = config.max_workers,
            )
            print(nougat_summary)
        except Exception as exc:
            logger.error("Nougat initialization/extraction failed: %s", exc)
            print(f"[Step 1] Nougat failed ({exc}). Falling back to lightweight extraction.")
            lw_summary, processed_basenames = lightweight_extract_pdfs(
                only_process_these = new_pdfs,
                output_dir         = output_dir,
                chunk_size         = config.chunk_size,
                chunk_overlap      = config.chunk_overlap,
                max_workers        = config.max_workers,
            )
            print(lw_summary)
        else:
            processed_set = set(processed_basenames)
            remaining = [p for p in new_pdfs if os.path.basename(p) not in processed_set]
            if remaining:
                logger.warning(
                    "Nougat produced no text for %d PDF(s). Falling back to lightweight extraction.",
                    len(remaining),
                )
                lw_summary, lw_processed = lightweight_extract_pdfs(
                    only_process_these = remaining,
                    output_dir         = output_dir,
                    chunk_size         = config.chunk_size,
                    chunk_overlap      = config.chunk_overlap,
                    max_workers        = config.max_workers,
                )
                print(lw_summary)
                processed_basenames.extend(
                    [name for name in lw_processed if name not in processed_set]
                )

    else:  # "lightweight"
        print("[Step 1] Running lightweight (PyMuPDF) text extraction ...")
        from utils.vp_text_extractor import lightweight_extract_pdfs

        lw_summary, processed_basenames = lightweight_extract_pdfs(
            only_process_these = new_pdfs,
            output_dir         = output_dir,
            chunk_size         = config.chunk_size,
            chunk_overlap      = config.chunk_overlap,
            max_workers        = config.max_workers,
        )
        print(lw_summary)
    summary["processed_basenames"] = processed_basenames

    # -----------------------------------------------------------------------
    # Step 2 Ã¢â‚¬â€ Figure description (Vision LLM, page-by-page)
    # -----------------------------------------------------------------------
    pdfs_for_figures = [
        p for p in new_pdfs
        if os.path.basename(p) in processed_basenames
    ]

    if pdfs_for_figures:
        print(f"[Step 2] Describing figures in {len(pdfs_for_figures)} PDF(s) Ã¢â‚¬Â¦")
        describe_figures_for_new_pdfs(
            new_pdf_paths    = pdfs_for_figures,
            output_dir       = output_dir,
            vision_provider  = config.vision_provider,
            vision_api_key   = _vision_api_key,
            vision_model     = _vision_model,
            vision_detail    = config.vision_detail,
            reasoning_effort = config.gpt_reasoning_effort,
        )
    else:
        print("[Step 2] No PDFs were successfully text-extracted; skipping figure description.")

    # -----------------------------------------------------------------------
    # Step 3 Ã¢â‚¬â€ Write metadata JSONL  (only for successfully processed PDFs)
    # -----------------------------------------------------------------------
    print("[Step 3] Writing document metadata Ã¢â‚¬Â¦")
    processed_set  = set(processed_basenames)
    metadata_rows: List[dict] = []
    for pdf_path, meta in pdf_meta_map.items():
        source = os.path.basename(pdf_path)
        if source not in processed_set:
            # Text extraction failed for this PDF Ã¢â‚¬â€ skip metadata too
            # so a failed run doesn't leave orphaned metadata records.
            logger.warning("Skipping metadata for %s (text extraction failed).", source)
            continue
        document_id = make_document_id(source)
        row         = {"source": source, "document_id": document_id}
        if isinstance(meta, dict):
            row.update(meta)
        metadata_rows.append(row)

    if metadata_rows:
        metadata_path = os.path.join(output_dir, "03_metadata_kb.jsonl")
        append_to_jsonl(metadata_path, metadata_rows)
        summary["metadata_written"] = len(metadata_rows)
        print(f"[Step 3] Wrote {len(metadata_rows)} metadata record(s).")

    # -----------------------------------------------------------------------
    # Step 4 Ã¢â‚¬â€ Persist the processing registry
    # -----------------------------------------------------------------------
    print("[Step 4] Updating processed-PDFs registry Ã¢â‚¬Â¦")
    mark_as_processed(registry_path, processed_basenames)

    # -----------------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------------
    chunks_path  = os.path.join(output_dir, "01_chunks_kb.jsonl")
    figures_path = os.path.join(output_dir, "02_visuals_kb.jsonl")

    def _count_lines(path: str) -> int:
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    summary["text_chunks_written"] = _count_lines(chunks_path)
    summary["figures_written"]     = _count_lines(figures_path)
    
    print("\n" + "=" * 60)
    print("Visual-Parser Pipeline Complete")
    print(f"  Total PDFs processed  : {len(processed_basenames)}")
    print(f"  Total Text chunks     : {summary['text_chunks_written']}")
    print(f"  Total Figure records  : {summary['figures_written']}")
    print(f"  Total Metadata records: {summary['metadata_written']}")
    print(f"  Output directory: {output_dir}")
    print("=" * 60)

    return summary


