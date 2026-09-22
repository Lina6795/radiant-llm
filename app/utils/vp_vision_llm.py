"""
vision_llm.py — Thin, cb-free wrapper around OpenAI and Google Gemini vision APIs.

Model routing
-------------
When the user picks provider "gpt" or "gemini" without specifying a model,
the pipeline defaults to the most capable current model for each provider:

    gpt    → gpt-5.2          (also accepts: gpt-5, gpt-5.1, gpt-4o, gpt-4.1)
    gemini → gemini-3-pro-preview  (also accepts: gemini-2.5-flash, gemini-1.5-pro)

GPT-5.x models
--------------
The gpt-5 / gpt-5.1 / gpt-5.2 family supports a ``reasoning_effort`` parameter
(none | low | medium | high | xhigh) instead of temperature.  This wrapper
detects the model family and adds the parameter automatically.
xhigh is only valid for gpt-5.2.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import List, Literal, Optional

from PIL import Image

logger = logging.getLogger(__name__)

DetailLevel = Literal["low", "high", "auto"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]

# Models in the GPT-5 family that support reasoning_effort
_GPT5_FAMILY = {"gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.3-chat-latest", "gpt-5.4", "gpt-5.4-2026-03-05"}

# Latest default model per provider
LATEST_GPT_MODEL    = "gpt-5.4"
LATEST_GEMINI_MODEL = "gemini-3-pro-preview"


def _is_gpt5(model: str) -> bool:
    """Return True when *model* belongs to the GPT-5 reasoning family."""
    m = model.lower()
    return m in _GPT5_FAMILY or m.startswith("gpt-5")


# ---------------------------------------------------------------------------
# OpenAI / GPT
# ---------------------------------------------------------------------------

def call_vision_llm_gpt(
    images: List[bytes],
    prompt: str,
    api_key: str,
    model: str = LATEST_GPT_MODEL,
    detail: DetailLevel = "low",
    reasoning_effort: Optional[ReasoningEffort] = "medium",
) -> str:
    """
    Send *images* (PNG bytes) and *prompt* to an OpenAI vision model.

    For the GPT-5 family (gpt-5, gpt-5.1, gpt-5.2) the ``reasoning_effort``
    parameter is passed to the API instead of temperature.
    ``xhigh`` is only valid for gpt-5.2.

    Args:
        images:           List of raw PNG byte strings.
        prompt:           Text instruction for the model.
        api_key:          OpenAI API key.
        model:            Vision-capable model name.
        detail:           Image resolution hint ('low', 'high', or 'auto').
        reasoning_effort: Reasoning depth for GPT-5.x models.
                          Ignored for older models (gpt-4o, gpt-4.1 …).

    Returns:
        Model response as a plain string.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package not installed. Run: pip install openai") from exc

    if not api_key:
        raise RuntimeError("OpenAI API key is not set.")

    client = OpenAI(api_key=api_key)

    # Build the multimodal message content
    content = [{"type": "text", "text": prompt}]
    for img_bytes in images:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64}",
                "detail": detail,
            },
        })

    # Build API call kwargs
    call_kwargs: dict = {
        "model":    model,
        "messages": [{"role": "user", "content": content}],
    }

    if _is_gpt5(model):
        # GPT-5 series: use reasoning_effort; temperature is not supported
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
        logger.info("[GPT-5] Using model=%s reasoning_effort=%s", model, reasoning_effort)
    else:
        # Older models: standard temperature
        call_kwargs["temperature"] = 0

    try:
        response = client.chat.completions.create(**call_kwargs)
        return response.choices[0].message.content.strip()
    except Exception as exc:
        raise RuntimeError(f"OpenAI vision call failed (model={model}): {exc}") from exc


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

def call_vision_llm_gemini(
    images: List[bytes],
    prompt: str,
    api_key: str,
    model: str = LATEST_GEMINI_MODEL,
) -> str:
    """
    Send *images* (PNG bytes) and *prompt* to a Google Gemini vision model.

    Args:
        images:  List of raw PNG byte strings.
        prompt:  Text instruction for the model.
        api_key: Gemini API key.
        model:   Gemini model name.

    Returns:
        Model response as a plain string.
    """
    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise RuntimeError(
            "google-generativeai package not installed. "
            "Run: pip install google-generativeai"
        ) from exc

    if not api_key:
        raise RuntimeError("Gemini API key is not set.")

    genai.configure(api_key=api_key)
    vision_model = genai.GenerativeModel(model)

    pil_images = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
    logger.info("[GEMINI] Using model=%s", model)

    try:
        response = vision_model.generate_content([prompt] + pil_images)
        return response.text
    except Exception as exc:
        raise RuntimeError(f"Gemini vision call failed (model={model}): {exc}") from exc


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def call_vision_llm(
    images: List[bytes],
    prompt: str,
    provider: str,
    api_key: str,
    model: str,
    detail: DetailLevel = "low",
    reasoning_effort: Optional[ReasoningEffort] = "medium",
) -> str:
    """
    Unified vision-LLM dispatcher.

    Automatically routes to the correct provider backend.  When *model* is
    empty or None, falls back to the latest default for that provider.

    Args:
        images:           List of raw PNG byte strings.
        prompt:           Text instruction for the model.
        provider:         ``'gpt'`` or ``'gemini'``.
        api_key:          API key for the chosen provider.
        model:            Model name string.
        detail:           Image detail level (GPT only; ignored for Gemini).
        reasoning_effort: Reasoning depth for GPT-5.x (ignored for older GPT
                          models and all Gemini models).

    Returns:
        Model response as a plain string.
    """
    resolved_model = model or (
        LATEST_GPT_MODEL if provider == "gpt" else LATEST_GEMINI_MODEL
    )

    if provider == "gpt":
        return call_vision_llm_gpt(
            images, prompt, api_key,
            model=resolved_model,
            detail=detail,
            reasoning_effort=reasoning_effort,
        )
    if provider == "gemini":
        return call_vision_llm_gemini(images, prompt, api_key, model=resolved_model)

    raise RuntimeError(
        f"Unknown vision provider: {provider!r}. Must be 'gpt' or 'gemini'."
    )
