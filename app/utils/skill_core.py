from __future__ import annotations

"""
Shared skill-loading core primitives.

This module is the single source of truth for app-agnostic skill routing logic:
tokenization, frontmatter parsing, hybrid candidate scoring, corpus document
frequencies, file-path safety checks, addon-pack qualification, and context
assembly helpers.

It is intended to be *vendored verbatim* across AutoFLUKA, AutoSAM, and
RADIANT-LLM. The canonical owner is AutoFLUKA (`utils/skill_core.py`); copies in
other apps must match byte-for-byte (see `tests/test_skill_core_sync` and
`developer_scripts/sync_skill_core.py`).

Design rule: nothing in here may import app-specific keyword tables, module maps,
preset configs, or security back-ends. Anything app-specific is passed in by the
caller (stopword sets, structural tokens, domain-generic tokens, route-score
thresholds, etc.). This keeps the routing math identical across apps while each
app keeps its own catalog, presets, and security wiring.
"""

import re
from pathlib import Path
from typing import Callable, Iterable, List

# --- Scoring / routing numeric defaults (shared across apps) ----------------
MIN_TOKEN_LEN = 2
DF_GENERIC_RATIO = 0.5
MIN_CORPUS_FOR_DF = 4
MIN_AUTO_ROUTE_SCORE = 2
MIN_USER_ADDON_ROUTE_SCORE = 4
ROUTING_KEYWORD_BOOST = 4  # RADIANT-LLM parity: +4 per keyword substring hit
CONTENT_SCORE_CHARS = 4096

# Generic query words stripped before any token overlap is measured. Apps may
# extend this set, but the shared baseline keeps routing math identical.
DEFAULT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "describe",
        "explain",
        "for",
        "from",
        "give",
        "have",
        "help",
        "high",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "level",
        "low",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "should",
        "show",
        "that",
        "the",
        "this",
        "to",
        "use",
        "using",
        "what",
        "when",
        "with",
        "would",
        "you",
    }
)

# Structural path tokens (folder/file scaffolding words) that should never carry
# routing authority on their own.
DEFAULT_PATH_STRUCTURAL_TOKENS = frozenset(
    {
        "index",
        "md",
        "readme",
        "reference",
        "references",
        "rules",
        "scripts",
        "skill",
        "skills",
        "txt",
    }
)

# Directory names that are never traversed when discovering skill files.
DEFAULT_BLOCKED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "venv",
        ".venv",
        "env",
        ".env",
        "chroma",
        "vector_db",
        "cache",
    }
)

# Extensions that always carry data/binary risk regardless of allowlist.
BLOCKED_DATA_EXTENSIONS = frozenset(
    {".jsonl", ".db", ".sqlite", ".sqlite3", ".bin", ".pkl"}
)

# Structural path priors: substring -> bonus weight. These bias *ranking* toward
# example/template/recipe material but must NOT, on their own, admit a lane (see
# `ScoringEngine.routing_evidence_score`). Apps with different folder conventions
# override these via `ScoringEngine(..., name_prior_bonuses=, path_prior_bonuses=)`.
# `name_prior_bonuses` match against ``path.name``; `path_prior_bonuses` match
# against the full path string.
DEFAULT_NAME_PRIOR_BONUSES = {"template": 1}
DEFAULT_PATH_PRIOR_BONUSES = {"working_examples": 1, "recipes": 1, "examples": 1}

# Generic English / task words that must not, on their own, route an unrelated
# user addon pack (e.g. "print out a minimum code" pulling a doc/movie pack).
# App-agnostic baseline: ONLY domain-neutral English. Domain-flavoured terms
# (e.g. "boundary", "phase") belong in each app's own domain-generic set, not
# here, so they are not wrongly suppressed for an app where they are meaningful.
DEFAULT_COMMON_TASK_TOKENS = frozenset(
    {
        "code",
        "print",
        "output",
        "input",
        "file",
        "files",
        "document",
        "documents",
        "minimum",
        "minimal",
        "setting",
        "settings",
        "two",
        "same",
        "here",
        "just",
        "show",
        "make",
        "design",
        "build",
        "create",
        "write",
        "generate",
        "report",
        "section",
        "text",
        "data",
        "value",
        "values",
        "table",
        "list",
        "example",
        "examples",
    }
)


# --- IO + frontmatter primitives -------------------------------------------
def safe_read(path: Path, max_chars: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + "\n...[truncated]..."
    except Exception as exc:
        return f"[Could not read {path}: {exc}]"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    Parse simple YAML frontmatter used by skill packs.

    This intentionally supports only scalar ``key: value`` lines; unsupported
    YAML remains body text and has no routing authority.
    """
    raw = text or ""
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    meta: dict[str, str] = {}
    end_idx: int | None = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if match:
            key = match.group(1).strip().lower()
            value = match.group(2).strip().strip("\"'")
            meta[key] = value
    if end_idx is None:
        return {}, raw
    return meta, "\n".join(lines[end_idx + 1 :])


def skill_frontmatter(path: Path, max_chars: int = CONTENT_SCORE_CHARS) -> tuple[dict[str, str], str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return {}, ""
    return parse_frontmatter(content)


def append_block(chunks: List[str], title: str, body: str, char_budget: int) -> int:
    if char_budget <= 0:
        return 0
    section = f"\n\n## {title}\n{body}".strip()
    if len(section) <= char_budget:
        chunks.append(section)
        return len(section)
    clipped = section[: max(0, char_budget - 16)] + "\n...[truncated]..."
    chunks.append(clipped)
    return len(clipped)


# --- Tokenization -----------------------------------------------------------
def make_tokenizer(
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
    min_token_len: int = MIN_TOKEN_LEN,
    *,
    split_underscores: bool = False,
) -> Callable[[str], set[str]]:
    """
    Build a tokenizer bound to a stopword set.

    ``split_underscores=False`` (AutoFLUKA/RADIANT default) keeps ``_`` inside
    tokens so identifiers like ``source_newgen`` survive. ``split_underscores=True``
    (AutoSAM-style) treats ``_`` and ``-`` as separators before tokenizing.
    """
    if split_underscores:
        def _tokenize(text: str) -> set[str]:
            normalized = (text or "").lower().replace("_", " ").replace("-", " ")
            return {
                t
                for t in re.findall(r"[a-z0-9]+", normalized)
                if len(t) >= min_token_len and t not in stopwords
            }
    else:
        def _tokenize(text: str) -> set[str]:
            return {
                t
                for t in re.findall(r"[a-z0-9_]+", (text or "").lower())
                if len(t) >= min_token_len and t not in stopwords
            }
    return _tokenize


# --- Path safety ------------------------------------------------------------
def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def has_blocked_path_part(
    path: Path,
    blocked_dir_names: frozenset[str] | set[str] = DEFAULT_BLOCKED_DIR_NAMES,
) -> bool:
    for part in path.parts:
        lowered = part.lower()
        if lowered.startswith(".") or lowered in blocked_dir_names:
            return True
    return False


def candidate_path_text(path: Path) -> str:
    parts = " ".join(path.parts[-4:])
    return f"{path.stem} {parts}".replace("-", " ").replace("_", " ")


# --- Source-agnostic scoring primitives (shared by all apps) -----------------
def overlap_count(
    text: str,
    query_tokens: set[str],
    tokenize: Callable[[str], set[str]],
    *,
    remove_structural: bool = False,
    structural_tokens: frozenset[str] | set[str] = DEFAULT_PATH_STRUCTURAL_TOKENS,
) -> int:
    """Number of query tokens overlapping the tokens of ``text``."""
    tokens = tokenize(text)
    if remove_structural:
        tokens = tokens - set(structural_tokens)
    return len(tokens & query_tokens)


def content_match_count(
    body: str,
    query_tokens: set[str],
    tokenize: Callable[[str], set[str]],
    *,
    doc_frequencies: dict[str, int] | None = None,
    corpus_size: int = 0,
    df_generic_ratio: float = DF_GENERIC_RATIO,
    min_corpus_for_df: int = MIN_CORPUS_FOR_DF,
    max_chars: int = CONTENT_SCORE_CHARS,
) -> int:
    """
    Query/body token overlap with corpus-generic terms filtered out.

    A matched token is dropped when its document frequency ratio is >=
    ``df_generic_ratio`` (only once the corpus is large enough to be meaningful).
    """
    matches = tokenize((body or "")[:max_chars]) & query_tokens
    if doc_frequencies and corpus_size >= min_corpus_for_df:
        matches = {
            token
            for token in matches
            if (doc_frequencies.get(token, 0) / max(1, corpus_size)) < df_generic_ratio
        }
    return len(matches)


def doc_frequencies_from_texts(
    texts: Iterable[str],
    tokenize: Callable[[str], set[str]],
    *,
    max_chars: int = CONTENT_SCORE_CHARS,
) -> tuple[dict[str, int], int]:
    """Document frequencies + corpus size from already-loaded text bodies."""
    frequencies: dict[str, int] = {}
    corpus_size = 0
    for text in texts:
        tokens = tokenize((text or "")[:max_chars])
        if not tokens:
            continue
        corpus_size += 1
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
    return frequencies, corpus_size


def phrase_hit_count(query_text: str, phrases: Iterable[str]) -> int:
    """Count of keyword phrases occurring as substrings in the query."""
    lowered = (query_text or "").lower()
    return sum(1 for phrase in phrases if phrase and phrase.lower() in lowered)


# --- Hybrid scoring engine --------------------------------------------------
class ScoringEngine:
    """
    Closes over a tokenizer + structural-token set + DF thresholds so call sites
    stay argument-free. The math is identical to the per-app implementations it
    replaces; only the configuration is injected.
    """

    def __init__(
        self,
        tokenize: Callable[[str], set[str]],
        *,
        path_structural_tokens: frozenset[str] | set[str] = DEFAULT_PATH_STRUCTURAL_TOKENS,
        df_generic_ratio: float = DF_GENERIC_RATIO,
        min_corpus_for_df: int = MIN_CORPUS_FOR_DF,
        content_score_chars: int = CONTENT_SCORE_CHARS,
        name_prior_bonuses: dict[str, int] | None = None,
        path_prior_bonuses: dict[str, int] | None = None,
        keyword_weight: int = ROUTING_KEYWORD_BOOST,
        slug_weight: int = 2,
        metadata_weight: int = 2,
        content_weight: int = 1,
    ) -> None:
        self._tokenize = tokenize
        self._path_structural_tokens = path_structural_tokens
        self._df_generic_ratio = df_generic_ratio
        self._min_corpus_for_df = min_corpus_for_df
        self._content_score_chars = content_score_chars
        self._keyword_weight = keyword_weight
        self._slug_weight = slug_weight
        self._metadata_weight = metadata_weight
        self._content_weight = content_weight
        self._name_prior_bonuses = (
            dict(DEFAULT_NAME_PRIOR_BONUSES) if name_prior_bonuses is None else dict(name_prior_bonuses)
        )
        self._path_prior_bonuses = (
            dict(DEFAULT_PATH_PRIOR_BONUSES) if path_prior_bonuses is None else dict(path_prior_bonuses)
        )

    def structural_prior_score(self, path: Path) -> int:
        """Query-independent folder/name priors. Ranking only -- never a gate."""
        name_lower = path.name.lower()
        full_lower = str(path).lower()
        score = 0
        for substring, weight in self._name_prior_bonuses.items():
            if substring in name_lower:
                score += weight
        for substring, weight in self._path_prior_bonuses.items():
            if substring in full_lower:
                score += weight
        return score

    def candidate_overlap_score(self, path: Path, query_tokens: set[str]) -> int:
        """Query-evidence portion of the path score (no structural priors)."""
        return overlap_count(
            candidate_path_text(path),
            query_tokens,
            self._tokenize,
            remove_structural=True,
            structural_tokens=self._path_structural_tokens,
        ) * self._slug_weight

    def candidate_score(self, path: Path, query_tokens: set[str]) -> int:
        return self.candidate_overlap_score(path, query_tokens) + self.structural_prior_score(path)

    def generic_content_tokens(self, doc_frequencies: dict[str, int] | None, corpus_size: int) -> set[str]:
        if not doc_frequencies or corpus_size < self._min_corpus_for_df:
            return set()
        return {
            token
            for token, count in doc_frequencies.items()
            if count / max(1, corpus_size) >= self._df_generic_ratio
        }

    def content_candidate_score(
        self,
        path: Path,
        query_tokens: set[str],
        max_chars: int | None = None,
        *,
        doc_frequencies: dict[str, int] | None = None,
        corpus_size: int = 0,
    ) -> int:
        if max_chars is None:
            max_chars = self._content_score_chars
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except Exception:
            return 0
        _meta, body = parse_frontmatter(content)
        return content_match_count(
            body,
            query_tokens,
            self._tokenize,
            doc_frequencies=doc_frequencies,
            corpus_size=corpus_size,
            df_generic_ratio=self._df_generic_ratio,
            min_corpus_for_df=self._min_corpus_for_df,
            max_chars=max_chars,
        )

    def description_candidate_score(self, path: Path, query_tokens: set[str]) -> int:
        meta, _body = skill_frontmatter(path)
        description = meta.get("description", "")
        if not description:
            return 0
        return overlap_count(description, query_tokens, self._tokenize) * self._metadata_weight

    def routing_evidence_score(
        self,
        path: Path,
        query_tokens: set[str],
        *,
        doc_frequencies: dict[str, int] | None = None,
        corpus_size: int = 0,
        description: str = "",
        max_chars: int | None = None,
    ) -> int:
        """
        Query-relevance score with structural priors EXCLUDED.

        Use this for admission/gating decisions so a lane cannot be selected on
        folder-name priors alone (e.g. a ``working_examples`` entrypoint whose
        path trivially contains "examples").
        """
        if max_chars is None:
            max_chars = self._content_score_chars
        score = self.candidate_overlap_score(path, query_tokens)
        if description:
            score += overlap_count(description, query_tokens, self._tokenize) * self._metadata_weight
        else:
            score += self.description_candidate_score(path, query_tokens)
        score += self.content_candidate_score(
            path,
            query_tokens,
            max_chars=max_chars,
            doc_frequencies=doc_frequencies,
            corpus_size=corpus_size,
        )
        return score

    def routing_candidate_score(
        self,
        path: Path,
        query_tokens: set[str],
        *,
        doc_frequencies: dict[str, int] | None = None,
        corpus_size: int = 0,
        description: str = "",
        max_chars: int | None = None,
    ) -> int:
        """Full ranking score: query evidence + structural priors."""
        return self.routing_evidence_score(
            path,
            query_tokens,
            doc_frequencies=doc_frequencies,
            corpus_size=corpus_size,
            description=description,
            max_chars=max_chars,
        ) + self.structural_prior_score(path)

    def routing_score_from_parts(
        self,
        *,
        slug_text: str,
        metadata_text: str,
        body: str,
        query_tokens: set[str],
        doc_frequencies: dict[str, int] | None = None,
        corpus_size: int = 0,
        keyword_phrases: Iterable[str] = (),
        query_text: str = "",
        score_bonus: int = 0,
        max_chars: int | None = None,
    ) -> int:
        """
        Hybrid routing score for an in-memory candidate (text already loaded).

        Mirrors the path-based routing math but sources slug/metadata/body text
        directly, so apps using a candidate-object model share the same engine:
        ``keyword*kw + slug_overlap*slug + metadata_overlap*meta + content*content + bonus``.
        """
        if max_chars is None:
            max_chars = self._content_score_chars
        keyword_score = phrase_hit_count(query_text, keyword_phrases)
        slug_score = overlap_count(
            slug_text,
            query_tokens,
            self._tokenize,
            remove_structural=True,
            structural_tokens=self._path_structural_tokens,
        )
        metadata_score = overlap_count(metadata_text, query_tokens, self._tokenize)
        content_score = content_match_count(
            body,
            query_tokens,
            self._tokenize,
            doc_frequencies=doc_frequencies,
            corpus_size=corpus_size,
            df_generic_ratio=self._df_generic_ratio,
            min_corpus_for_df=self._min_corpus_for_df,
            max_chars=max_chars,
        )
        return (
            keyword_score * self._keyword_weight
            + slug_score * self._slug_weight
            + metadata_score * self._metadata_weight
            + content_score * self._content_weight
            + score_bonus
        )

    def corpus_doc_frequencies(
        self,
        paths: Iterable[Path],
        max_chars: int | None = None,
    ) -> tuple[dict[str, int], int]:
        if max_chars is None:
            max_chars = self._content_score_chars
        frequencies: dict[str, int] = {}
        corpus_size = 0
        seen_paths: set[str] = set()
        for path in paths:
            try:
                key = str(path.resolve())
            except Exception:
                key = str(path)
            if key in seen_paths or not path.is_file():
                continue
            seen_paths.add(key)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except Exception:
                continue
            meta, body = parse_frontmatter(content)
            tokens = self._tokenize(body + " " + meta.get("description", ""))
            if not tokens:
                continue
            corpus_size += 1
            for token in tokens:
                frequencies[token] = frequencies.get(token, 0) + 1
        return frequencies, corpus_size


# --- User addon pack qualification -----------------------------------------
def user_addon_pack_tokens(path: Path, tokenize: Callable[[str], set[str]]) -> set[str]:
    meta, _ = skill_frontmatter(path)
    slug = path.parent.name
    slug_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", slug)
    slug_spaced = slug_spaced.replace("-", " ").replace("_", " ")
    name = meta.get("name", "")
    description = meta.get("description", "")
    return tokenize(f"{name} {slug_spaced} {description}")


def qualifies_user_addon_pack(
    path: Path,
    score: int,
    query: str,
    *,
    tokenize: Callable[[str], set[str]],
    domain_generic_tokens: frozenset[str] | set[str] = frozenset(),
    common_task_tokens: frozenset[str] | set[str] = frozenset(),
    min_auto_route_score: int = MIN_AUTO_ROUTE_SCORE,
    min_addon_route_score: int = MIN_USER_ADDON_ROUTE_SCORE,
    content_score_chars: int = CONTENT_SCORE_CHARS,
) -> bool:
    """Require pack slug/name/description overlap or a stronger non-generic content match."""
    query_tokens = tokenize(query)
    pack_tokens = user_addon_pack_tokens(path, tokenize)
    if pack_tokens & query_tokens:
        return score >= min_auto_route_score
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:content_score_chars]
    except Exception:
        return False
    _meta, body = parse_frontmatter(content)
    distinctive = (
        (tokenize(body) & query_tokens)
        - domain_generic_tokens
        - common_task_tokens
    )
    if len(distinctive) >= 2:
        return score >= min_auto_route_score
    if len(distinctive) >= 1:
        return score >= min_addon_route_score
    return False


# --- Alert / UI labels ------------------------------------------------------
_TRAILING_SKILL_RE = re.compile(r"\s+skills?$", re.IGNORECASE)


def consulting_skill_label(skill_name: str) -> str:
    """
    Uniform 'Consulting: <name> Skills' alert label, shared across all apps so a
    consulted pack reads the same whether it is a developer/bundled module or a
    user addon pack.

    A trailing 'Skill'/'Skills' already present in the pack title is stripped so
    titles that end in 'Skill' do not render as '... Skill Skills'.
    """
    clean = (skill_name or "Skill").strip()
    clean = _TRAILING_SKILL_RE.sub("", clean).strip() or "Skill"
    return f"Consulting: {clean} Skills"
