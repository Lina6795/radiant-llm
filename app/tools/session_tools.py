"""
RADIANT-LLM — Session search tool for the agent.

`make_search_past_sessions_tool(chatbot)` returns a LangChain @tool that
searches all past sessions using hybrid fuzzy+TF-IDF cosine scoring.
The tool is user-triggered only — the agent calls it when the user explicitly
asks about prior work, earlier sessions, or past findings.
"""
import datetime
from typing import Annotated

from langchain_core.tools import tool


def _fmt_ts(ms: int) -> str:
    """Convert epoch-milliseconds to a readable date string."""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except Exception:
        return "unknown date"


def make_search_past_sessions_tool(chatbot):
    """
    Factory that closes over the Chatbot instance and returns a @tool function.
    Call once during initialize_models(); append result to self.agent_tools.
    """

    @tool
    def search_past_sessions(
        query: Annotated[
            str,
            "Search query describing what you are looking for in past sessions.",
        ],
    ) -> str:
        """Search all past conversation sessions for relevant context.

        Use this tool when the user explicitly asks about:
        - Prior work or earlier sessions ("what were we doing last time?")
        - Past findings or values established in previous chats
        - Continuing a topic from a session that is no longer loaded
        - Any reference to 'before', 'previously', 'last session', 'earlier', etc.

        Returns matched turns with session title, date, relevance score, and
        an optional session summary. Do NOT call this on every query — only when
        the user clearly wants cross-session context.
        """
        from utils.session_store import search_all_sessions

        try:
            sd = chatbot._get_session_dir()
        except Exception:
            return "Session directory not available. Initialize a model first."

        results = search_all_sessions(
            sd,
            query,
            exclude_session_id=chatbot._active_session_id,
            top_k=10,
        )

        if not results:
            return (
                "No relevant past sessions found for that query. "
                "Sessions that have been summarized (via the 'Summarize memory' button) "
                "are searched more accurately."
            )

        lines = ["[PAST SESSION CONTEXT]", ""]
        for i, r in enumerate(results, 1):
            date = _fmt_ts(r.get("session_created_at", 0))
            title = r.get("session_title", "Untitled")
            score = r.get("score", 0.0)
            sess_summary = (r.get("session_summary") or "").strip()

            lines.append(f"--- Match {i} | '{title}' | {date} | relevance {score:.2f} ---")
            if sess_summary:
                preview = sess_summary[:200] + ("…" if len(sess_summary) > 200 else "")
                lines.append(f"Session summary: {preview}")
            q_text = (r.get("query") or "").strip()
            a_text = (r.get("response") or "").strip()
            if len(a_text) > 400:
                a_text = a_text[:397] + "…"
            lines.append(f"User asked: {q_text}")
            lines.append(f"Assistant replied: {a_text}")
            lines.append("")

        lines.append("[END PAST SESSION CONTEXT]")
        return "\n".join(lines)

    return search_past_sessions
