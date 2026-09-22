"""Grace HPRC vLLM helpers for RADIANT-LLM (SSH tunnel + OpenAI-compatible API)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

import requests
from langchain_openai import ChatOpenAI

@dataclass(frozen=True)
class GraceModelVariant:
    """One servable Grace vLLM model: id, default checkpoint/job, and quantization mode."""

    id: str
    label: str
    model_folder: str  # checkpoint folder name only; full path = GRACE_MODELS_DIR / model_folder
    job_name: str
    sbatch_filename: str
    default_local_port: int
    quantization: str  # "none" | "fp8" | "bitsandbytes"


GRACE_MODEL_VARIANTS: dict[str, GraceModelVariant] = {
    "grace-gemma-4-31b": GraceModelVariant(
        id="grace-gemma-4-31b",
        label="Grace Gemma 4 31B (local, bf16)",
        model_folder="gemma-4-31B-it",
        job_name="gemma4-31b-vllm",
        sbatch_filename="run_gemma4_31b_vllm.sbatch",
        default_local_port=8001,
        quantization="none",
    ),
    "grace-gemma-4-31b-fp8": GraceModelVariant(
        id="grace-gemma-4-31b-fp8",
        label="Grace Gemma 4 31B — FP8 quantized (local)",
        model_folder="gemma-4-31B-it",
        job_name="gemma4-31b-quant-vllm",
        sbatch_filename="run_gemma4_31b_quant_vllm.sbatch",
        default_local_port=8002,
        quantization="fp8",
    ),
    "grace-gemma-4-26b-a4b-fp8": GraceModelVariant(
        id="grace-gemma-4-26b-a4b-fp8",
        label="Grace Gemma 4 26B-A4B — FP8 quantized (local)",
        model_folder="gemma-4-26B-A4B-it",
        job_name="gemma4-26b-a4b-quant-vllm",
        sbatch_filename="run_gemma4_26b_a4b_quant_vllm.sbatch",
        default_local_port=8003,
        quantization="fp8",
    ),
}
# grace-gemma-4-12b-fp8 removed: gemma4_unified architecture (audio+vision) has no native
# vLLM 0.21.0 backend; Transformers fallback + FP8 Marlin produces Int dtype in LayerNorm
# during KV cache profiling → NotImplementedError. Re-add when vLLM adds native support.

# Default/legacy id: kept as a plain constant since other modules import it directly
# (e.g. developer_scripts/verify_local_grace_init.py).
GRACE_MODEL_ID = "grace-gemma-4-31b"


def _env_suffix(variant_id: str) -> str:
    return variant_id.upper().replace("-", "_").replace(".", "_")


# Shown when vLLM rejects OpenAI-style tool_choice=auto (RADIANT agents use native tool calling).
GRACE_VLLM_NATIVE_TOOLS_HINT = (
    "On Grace, add to the `vllm serve` line in the sbatch job for this variant: "
    "`--enable-auto-tool-choice` and `--tool-call-parser gemma4`. "
    "Then `scancel` the old job, `sbatch` again, wait until vLLM is listening, "
    "and reopen the SSH tunnel to the new NODELIST. "
    "See developer_scripts/README.md for sbatch templates and setup details."
)

_TUNNEL_HELP = (
    "Start Grace vLLM and the SSH tunnel for this variant first:\n"
    "  ./developer_scripts/start_grace_gemma_tunnel.sh --variant <model-id>\n"
    "Then verify: curl http://localhost:<port>/v1/models\n"
    "See developer_scripts/README.md for setup details."
)


@dataclass(frozen=True)
class VllmConfig:
    base_url: str
    api_key: str
    model: str


def load_vllm_config(variant_id: str = GRACE_MODEL_ID) -> VllmConfig:
    """
    Load vLLM connection config for a Grace model variant.

    `VLLM_API_KEY` is shared across all variants (every sbatch job uses the same
    server key). `VLLM_BASE_URL` / `VLLM_MODEL` fall back to the registry's
    per-variant defaults but can be overridden per variant via
    `VLLM_BASE_URL__<VARIANT_ID>` / `VLLM_MODEL__<VARIANT_ID>` in `.env`
    (e.g. `VLLM_BASE_URL__GRACE_GEMMA_4_31B_FP8`). The legacy unsuffixed
    `VLLM_BASE_URL` / `VLLM_MODEL` are honored for the default variant only,
    so existing `.env` files keep working unchanged.
    """
    variant = GRACE_MODEL_VARIANTS.get(variant_id)
    if variant is None:
        raise ValueError(f"Unknown Grace model variant: {variant_id!r}")

    suffix = _env_suffix(variant_id)
    api_key = os.getenv("VLLM_API_KEY")
    if not (api_key and api_key.strip()):
        raise ValueError(
            "Missing required environment variable: VLLM_API_KEY. "
            "Add it to .env (see .env.example)."
        )

    base_url = os.getenv(f"VLLM_BASE_URL__{suffix}")
    if not base_url and variant_id == GRACE_MODEL_ID:
        base_url = os.getenv("VLLM_BASE_URL")
    if not base_url:
        base_url = f"http://localhost:{variant.default_local_port}/v1"

    model = os.getenv(f"VLLM_MODEL__{suffix}")
    if not model and variant_id == GRACE_MODEL_ID:
        model = os.getenv("VLLM_MODEL")
    if not model:
        models_dir = os.getenv("GRACE_MODELS_DIR", "").strip().rstrip("/")
        if not models_dir:
            raise ValueError(
                f"Cannot resolve model path for variant {variant_id!r}. "
                "Set GRACE_MODELS_DIR in .env "
                "(e.g. GRACE_MODELS_DIR=/scratch/user/your_netid/local_llm/models) "
                "or override per-variant with VLLM_MODEL__<VARIANT_ID>. "
                "See .env.example for details."
            )
        model = f"{models_dir}/{variant.model_folder}"

    return VllmConfig(base_url=base_url.rstrip("/"), api_key=api_key, model=model)


def check_vllm_reachable(
    config: Optional[VllmConfig] = None,
    timeout: float = 30,
) -> Tuple[bool, str, Optional[dict[str, Any]]]:
    """
    Probe GET /v1/models. Returns (ok, message, models_json).
    """
    cfg = config or load_vllm_config()
    url = f"{cfg.base_url}/models"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        return (
            False,
            f"Cannot reach vLLM at {cfg.base_url}: connection refused.\n\n{_TUNNEL_HELP}",
            None,
        )
    except requests.exceptions.Timeout:
        return (
            False,
            f"Cannot reach vLLM at {cfg.base_url}: connection timed out.\n\n{_TUNNEL_HELP}",
            None,
        )
    except requests.RequestException as exc:
        return False, f"Cannot reach vLLM at {cfg.base_url}: {exc}\n\n{_TUNNEL_HELP}", None

    if resp.status_code == 401:
        return (
            False,
            "vLLM returned 401 Unauthorized. Check VLLM_API_KEY in .env "
            f"(expected server key, not OPENAI_API_KEY).\n\n{_TUNNEL_HELP}",
            None,
        )

    if not resp.ok:
        body = resp.text[:500]
        return (
            False,
            f"vLLM models check failed ({resp.status_code}): {body}\n\n{_TUNNEL_HELP}",
            None,
        )

    try:
        data = resp.json()
    except ValueError:
        return False, f"vLLM returned non-JSON from {url}\n\n{_TUNNEL_HELP}", None

    model_ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    if cfg.model not in model_ids:
        return (
            False,
            f"VLLM_MODEL={cfg.model!r} not in server models: {model_ids}\n\n{_TUNNEL_HELP}",
            data,
        )

    return True, "vLLM reachable.", data


def get_vllm_max_model_len(
    config: Optional[VllmConfig] = None,
    models_json: Optional[dict[str, Any]] = None,
) -> int:
    """
    Effective context length for the served model.

    Prefer VLLM_MAX_MODEL_LEN in .env if set; else read max_model_len from GET /v1/models.
    """
    env_val = os.getenv("VLLM_MAX_MODEL_LEN", "").strip()
    if env_val.isdigit():
        return int(env_val)

    data = models_json
    if data is None:
        cfg = config or load_vllm_config()
        ok, _, data = check_vllm_reachable(cfg)
        if not ok or not data:
            return 4096

    cfg = config or load_vllm_config()
    for entry in data.get("data", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == cfg.model or entry.get("root") == cfg.model:
            mlen = entry.get("max_model_len")
            if isinstance(mlen, int) and mlen > 0:
                return mlen
    # Fallback: first model entry
    for entry in data.get("data", []):
        if isinstance(entry, dict):
            mlen = entry.get("max_model_len")
            if isinstance(mlen, int) and mlen > 0:
                return mlen
    return 4096


# Subset of RADIANT tools for Grace (full list does not fit ~4k–5k vLLM windows).
GRACE_COMPACT_TOOL_NAMES: Tuple[str, ...] = (
    "TextFileReaderTool",
    "PythonREPLTool",
    "WikipediaSearchTool",
    "WebSearchTool",
    "WebScraperTool",
)

_GRACE_PROMPT_TRUNCATION_NOTE = (
    "\n\n[System prompt shortened for Grace vLLM context limit. "
    "Use a cloud GPT/Gemini model for the full RADIANT policy.]"
)

# Heuristic reserve: compact tool schemas + agent template + user message + generation.
_GRACE_RESERVED_TOKENS_FOR_AGENT = 2600


def grace_skill_context_char_budget(max_model_len: int) -> int:
    """
    Bundled skill-map injection budget (chars).

    On tight Grace windows (e.g. 4730), the default agent stack already consumes
    nearly the full context; omit bundled index/SKILL.md injection (budget 0).
    """
    if max_model_len <= 5120:
        return 0
    if max_model_len <= 8192:
        return 1500
    if max_model_len <= 16384:
        return 6000
    return 10_000


def grace_system_prompt_char_limit(max_model_len: int) -> int:
    """Max characters for the main system message on Grace."""
    slack_tokens = max(512, max_model_len - _GRACE_RESERVED_TOKENS_FOR_AGENT)
    return int(slack_tokens * 3)


def grace_system_prompt_for_context(full_prompt: str, max_model_len: int) -> str:
    """Truncate the YAML system prompt so a tool-calling agent fits the vLLM window."""
    limit = grace_system_prompt_char_limit(max_model_len)
    if len(full_prompt) <= limit:
        return full_prompt
    note = _GRACE_PROMPT_TRUNCATION_NOTE
    body_limit = max(0, limit - len(note))
    return full_prompt[:body_limit].rstrip() + note


def grace_tools(all_tools: Sequence[Any], max_model_len: int = 4096) -> list[Any]:
    """
    Return the tool set appropriate for the available context.

    > 8192 tokens: all tools (full agent capability).
    ≤ 8192 tokens: compact subset so tool schemas don't consume the window.
    """
    if max_model_len > 8192:
        return list(all_tools)
    by_name = {getattr(t, "name", None): t for t in all_tools}
    selected = [by_name[name] for name in GRACE_COMPACT_TOOL_NAMES if name in by_name]
    return selected if selected else list(all_tools)


def check_vllm_native_tool_support(
    config: Optional[VllmConfig] = None,
    timeout: float = 45,
) -> Tuple[bool, str]:
    """
    Probe POST /v1/chat/completions with tool_choice=auto (same requirement as LangChain agents).

    Returns (True, "") if the server accepts the request (tools usable).
    Returns (False, hint) if vLLM returns 400 about missing tool-choice flags.
    On network errors, returns (True, "") so Initialize is not blocked by a flaky probe.
    """
    cfg = config or load_vllm_config()
    url = f"{cfg.base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "_radiant_probe_noop",
                    "description": "Probe only; ignore.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException:
        return True, ""

    if resp.status_code == 200:
        return True, ""

    body = resp.text or ""
    if (
        resp.status_code == 400
        and "enable-auto-tool-choice" in body
        and "tool-call-parser" in body
    ):
        return False, GRACE_VLLM_NATIVE_TOOLS_HINT

    return True, ""


def build_grace_chat_openai(
    temperature: float = 0.7,
    streaming: bool = True,
    config: Optional[VllmConfig] = None,
    variant_id: str = GRACE_MODEL_ID,
) -> ChatOpenAI:
    """ChatOpenAI pointed at Grace vLLM (never uses OPENAI_API_KEY)."""
    cfg = config or load_vllm_config(variant_id)
    return ChatOpenAI(
        model=cfg.model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        temperature=temperature,
        streaming=streaming,
    )
