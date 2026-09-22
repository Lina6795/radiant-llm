"""
RADIANT-LLM — JSONL-backed session persistence.

Each session maps to one JSONL file in RADIANT_LLM_Sessions/.
index.json holds lightweight session metadata (no message content).
"""
import difflib
import json
import time
from pathlib import Path
from typing import List, Optional

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


# ── Session directory ──────────────────────────────────────────────────────────

def get_session_dir() -> Path:
    """
    Resolve the session storage directory.
    Checks RADIANT_LLM_SESSION_DIR env var first; falls back to
    RADIANT_LLM_Sessions/ as a sibling of the RADIANT_LLM_Logs directory.
    """
    import os
    env = os.getenv("RADIANT_LLM_SESSION_DIR", "").strip()
    if env:
        return Path(env)
    from utils.general_utilities import LOG_DIR  # lazy import avoids circular deps
    return LOG_DIR.parent / "RADIANT_LLM_Sessions"


# ── JSONL message history ──────────────────────────────────────────────────────

class JSONLChatMessageHistory(BaseChatMessageHistory):
    """LangChain-compatible chat history backed by a JSONL file (2 lines per turn)."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self.file_path.touch()

    @property
    def messages(self) -> List[BaseMessage]:
        result: List[BaseMessage] = []
        try:
            with self.file_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    content = rec.get("content", "")
                    if rec.get("type") == "human":
                        result.append(HumanMessage(content=content))
                    elif rec.get("type") == "ai":
                        result.append(AIMessage(content=content))
        except Exception:
            pass
        return result

    def add_message(self, message: BaseMessage) -> None:
        rec = {
            "type": "human" if isinstance(message, HumanMessage) else "ai",
            "content": (
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            ),
            "ts": int(time.time() * 1000),
        }
        with self.file_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        self.file_path.write_text("", encoding="utf-8")


class TruncatedJSONLChatMessageHistory(JSONLChatMessageHistory):
    """
    Same persistence as JSONLChatMessageHistory but caps the messages returned
    to the agent at the last `keep_last_n` turns (i.e. keep_last_n * 2 lines).
    New turns are still appended to the full file — only the agent's view is trimmed.
    """

    def __init__(self, file_path: Path, keep_last_n: int = 10) -> None:
        super().__init__(file_path)
        self._keep = keep_last_n

    @property
    def messages(self) -> List[BaseMessage]:
        all_msgs = super().messages
        if self._keep > 0:
            return all_msgs[-(self._keep * 2):]
        return all_msgs


# ── Index helpers ──────────────────────────────────────────────────────────────

def _index_path(session_dir: Path) -> Path:
    return session_dir / "index.json"


def load_index(session_dir: Path) -> List[dict]:
    idx = _index_path(session_dir)
    if not idx.exists():
        return []
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_index(session_dir: Path, entries: List[dict]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    _index_path(session_dir).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def upsert_index(session_dir: Path, entry: dict) -> None:
    """Insert or update one session entry in index.json (matched by id)."""
    entries = load_index(session_dir)
    for i, e in enumerate(entries):
        if e.get("id") == entry.get("id"):
            entries[i] = entry
            save_index(session_dir, entries)
            return
    entries.append(entry)
    save_index(session_dir, entries)


def remove_from_index(session_dir: Path, session_id: str) -> None:
    entries = [e for e in load_index(session_dir) if e.get("id") != session_id]
    save_index(session_dir, entries)


def session_jsonl_path(session_dir: Path, session_id: str) -> Path:
    return session_dir / f"{session_id}.jsonl"


def read_session_turns(session_dir: Path, session_id: str) -> List[dict]:
    """Return all turns as [{id, query, response, reasoningEvents}] for UI display."""
    path = session_jsonl_path(session_dir, session_id)
    if not path.exists():
        return []
    msgs = JSONLChatMessageHistory(path).messages
    turns: List[dict] = []
    i = 0
    while i + 1 < len(msgs):
        if isinstance(msgs[i], HumanMessage) and isinstance(msgs[i + 1], AIMessage):
            turns.append(
                {
                    "id": len(turns),
                    "query": msgs[i].content,
                    "response": msgs[i + 1].content,
                    "reasoningEvents": [],
                }
            )
            i += 2
        else:
            i += 1
    return turns


# ── Session summarization ─────────────────────────────────────────────────────

def summarize_session_turns(turns: List[dict], llm) -> str:
    """
    Call the LLM to produce a compact summary of a session's turns.
    `turns` is [{query, response, ...}] already cleaned for display.
    `llm` is the LangChain chat model instance (must be initialized).
    Returns the summary string, or "" on failure.
    """
    from langchain_core.messages import SystemMessage, HumanMessage as _HM

    if not turns or llm is None:
        return ""

    lines: List[str] = []
    for i, t in enumerate(turns, 1):
        q = (t.get("query") or "").strip()
        r = (t.get("response") or "").strip()
        if len(r) > 400:
            r = r[:397] + "…"
        lines.append(f"Turn {i}:")
        lines.append(f"  User: {q}")
        lines.append(f"  Assistant: {r}")
        lines.append("")

    system_prompt = (
        "You are a scientific research assistant summarizing a conversation session. "
        "Write a concise summary (150–250 words) that captures: "
        "(1) the main topics and questions discussed; "
        "(2) key findings, values, parameters, or file paths established; "
        "(3) decisions or conclusions reached; "
        "(4) what was being actively worked on at the end of the session. "
        "Be specific — preserve technical details. "
        "Write in past tense as a single continuous paragraph."
    )
    user_msg = "Summarize this conversation session:\n\n" + "\n".join(lines)

    try:
        result = llm.invoke([SystemMessage(content=system_prompt), _HM(content=user_msg)])
        content = getattr(result, "content", "") or ""
        if isinstance(content, list):
            # GPT-5.x returns content as [{'type': 'text', 'text': '...'}]
            content = " ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return content.strip()
    except Exception:
        return ""


# ── Text extraction helpers ────────────────────────────────────────────────────

def _extract_plain_text(raw: str) -> str:
    """
    Strip the LangChain agent output wrapper so search indexes plain text.

    Agent responses are stored in JSONL as the Python repr of a list of dicts:
      [{'type': 'text', 'text': 'The actual answer…'}]
    This function extracts just the inner text, falling back to `raw` unchanged.
    """
    if not raw or not raw.startswith("[{"):
        return raw
    try:
        import ast
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            parts = [item.get("text", "") for item in parsed if isinstance(item, dict)]
            extracted = " ".join(p for p in parts if p).strip()
            return extracted if extracted else raw
    except Exception:
        pass
    return raw


# ── Session history retrieval ──────────────────────────────────────────────────

def session_history_search(
    session_jsonl_path: Path,
    query: str,
    keep_last_n: int = 10,
    top_k: int = 4,
    threshold: float = 0.25,
) -> List[dict]:
    """
    Retrieve relevant past turns from a session JSONL using hybrid fuzzy + TF-IDF
    cosine scoring (same pattern as AutoSAM ERKB matching).

    score = max(fuzzy_ratio, cosine_tfidf)

    Turns in the verbatim tail (last keep_last_n) are excluded since the agent
    already sees them via ConversationBufferMemory. Returns up to top_k turns
    sorted by descending score.
    """
    # Try to import sklearn; fall back to fuzzy-only if unavailable.
    _sklearn_ok = False
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as _cos_sim
        _sklearn_ok = True
    except ImportError:
        pass

    if not session_jsonl_path.exists():
        return []

    # Read all Human/AI message pairs into turns.
    msgs = JSONLChatMessageHistory(session_jsonl_path).messages
    turns: List[dict] = []
    i = 0
    while i + 1 < len(msgs):
        if isinstance(msgs[i], HumanMessage) and isinstance(msgs[i + 1], AIMessage):
            turns.append({
                "turn_index": len(turns),
                "query": msgs[i].content,
                "response": msgs[i + 1].content,
            })
            i += 2
        else:
            i += 1

    # Exclude the verbatim tail already injected by TruncatedJSONLChatMessageHistory.
    # keep_last_n=0 means search ALL turns (used for cross-session search).
    if keep_last_n > 0:
        candidates = turns[:-keep_last_n] if len(turns) > keep_last_n else []
    else:
        candidates = turns
    if not candidates:
        return []

    query_lower = query.lower().strip()

    # Full exchange (human + ai) as the searchable unit — richer signal than query alone.
    # Strip the LangChain [{'type':'text','text':'...'}] wrapper so TF-IDF sees plain text.
    texts = [f"{t['query']} {_extract_plain_text(t['response'])}" for t in candidates]
    texts_lower = [t.lower() for t in texts]

    # Batch TF-IDF cosine: fit once on [query] + all candidate texts.
    cosine_scores: List[float] = [0.0] * len(candidates)
    if _sklearn_ok:
        try:
            vect = TfidfVectorizer(min_df=1, sublinear_tf=True).fit([query] + texts)
            tfidf = vect.transform([query] + texts)
            sims = _cos_sim(tfidf[0:1], tfidf[1:])[0]
            cosine_scores = sims.tolist()
        except Exception:
            pass

    # Per-turn fuzzy ratio + max fusion.
    results: List[dict] = []
    for idx, turn in enumerate(candidates):
        fr = difflib.SequenceMatcher(None, query_lower, texts_lower[idx]).ratio()
        cs = cosine_scores[idx]
        score = max(fr, cs)
        if score >= threshold:
            results.append({**turn, "score": round(score, 4)})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ── Cross-session search ───────────────────────────────────────────────────────

def search_all_sessions(
    session_dir: Path,
    query: str,
    exclude_session_id: Optional[str] = None,
    top_k: int = 10,
    threshold: float = 0.10,
    summary_prefilter_threshold: float = 0.06,
) -> List[dict]:
    """
    Hybrid cross-session search. For every past session in index.json:

    1. If the session has a stored summary, run a quick fuzzy check against it
       (summary_prefilter_threshold). Sessions whose summary clearly doesn't
       match are skipped (cheap gate — avoids scanning every JSONL).
    2. Sessions without a summary are always scanned (no false negatives).
    3. For sessions that pass: run session_history_search with keep_last_n=0
       (all turns) and collect scored hits.
    4. Merge, sort by score, return top_k tagged with session metadata.
    """
    entries = load_index(session_dir)
    if not entries:
        return []

    combined: List[dict] = []
    query_lower = query.lower().strip()

    for entry in entries:
        sid = entry.get("id")
        if not sid or sid == exclude_session_id:
            continue

        summary = (entry.get("summary") or "").strip()
        title = entry.get("title") or "New conversation"
        created_at = entry.get("createdAt", 0)

        # Summary pre-filter: skip clearly irrelevant sessions.
        if summary:
            summary_score = difflib.SequenceMatcher(
                None, query_lower, summary.lower()
            ).ratio()
            if summary_score < summary_prefilter_threshold:
                continue

        jsonl = session_jsonl_path(session_dir, sid)
        hits = session_history_search(
            jsonl, query, keep_last_n=0, top_k=3, threshold=threshold
        )
        for h in hits:
            combined.append(
                {
                    **h,
                    "session_id": sid,
                    "session_title": title,
                    "session_created_at": created_at,
                    "session_summary": summary,
                }
            )

    combined.sort(key=lambda x: x["score"], reverse=True)
    return combined[:top_k]
