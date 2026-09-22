"""Local Hugging Face embeddings for RADIANT-LLM (no cloud API)."""

from __future__ import annotations

import os

from langchain_core.embeddings import Embeddings

DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_LOCAL_EMBEDDING_DIM = 768


def build_local_embeddings(
    model_name: str | None = None,
    device: str | None = None,
) -> Embeddings:
    """
    Open-source embeddings via sentence-transformers (768-d for bge-base-en-v1.5).

    Env overrides:
      RADIANT_LOCAL_EMBEDDING_MODEL  (default BAAI/bge-base-en-v1.5)
      RADIANT_LOCAL_EMBEDDING_DEVICE (default cpu)
    """
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        except ImportError as exc:
            raise ImportError(
                "Local embeddings require sentence-transformers and either "
                "langchain-huggingface (>=0.1.2,<0.2) or langchain-community. "
                "Install: pip install sentence-transformers langchain-huggingface==0.1.2"
            ) from exc

    name = (model_name or os.getenv("RADIANT_LOCAL_EMBEDDING_MODEL") or DEFAULT_LOCAL_EMBEDDING_MODEL).strip()
    dev = (device or os.getenv("RADIANT_LOCAL_EMBEDDING_DEVICE") or "cpu").strip()

    return HuggingFaceEmbeddings(
        model_name=name,
        model_kwargs={"device": dev},
        encode_kwargs={"normalize_embeddings": True},
    )
