"""Spill large tool outputs to disk, keep only a pointer in context.

A tool result or bulky artifact that would swamp its partition quota is
written to the artifact directory (``RADIANT_ARTIFACT_DIR`` env var, or
an explicit root) and replaced in context by an :class:`ArtifactPointer`
carrying the URI, content hash, a short extractive summary and the
original size — enough to cite, verify and lazily re-fetch the content
via :meth:`ArtifactStore.resolve`.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Union

from .tokenizer import TokenCounter, default_counter

ARTIFACT_DIR_ENV = "RADIANT_ARTIFACT_DIR"
SUMMARY_CHARS = 160

_WS_RE = re.compile(r"\s+")


@dataclass
class ArtifactPointer:
    uri: str
    sha256: str
    size_bytes: int
    original_tokens: int
    summary: str
    kind: str

    def to_dict(self) -> dict:
        return asdict(self)

    def stub(self) -> str:
        """Compact in-context representation of the spilled content."""
        return (f"[{self.kind} artifact: uri={self.uri} "
                f"sha256={self.sha256[:12]} size={self.size_bytes}B "
                f"tokens={self.original_tokens} summary={self.summary!r}]")


class ArtifactStore:
    def __init__(self, root: Optional[Union[str, os.PathLike]] = None,
                 counter: Optional[TokenCounter] = None) -> None:
        if root is None:
            root = os.environ.get(ARTIFACT_DIR_ENV) or "artifact_store"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.counter = counter or default_counter()

    # ------------------------------------------------------------------
    @staticmethod
    def _summary(content: str) -> str:
        collapsed = _WS_RE.sub(" ", content).strip()
        if len(collapsed) <= SUMMARY_CHARS:
            return collapsed
        return collapsed[: SUMMARY_CHARS - 1].rstrip() + "…"

    def store(self, content: str, kind: str = "tool_result") -> ArtifactPointer:
        """Write ``content`` to disk and return its pointer."""
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        safe_kind = re.sub(r"[^a-z0-9_-]", "_", kind.lower())
        path = self.root / f"{safe_kind}-{digest[:16]}.txt"
        if not path.exists():  # content-addressed: same bytes, same file
            path.write_bytes(data)
        return ArtifactPointer(
            uri=f"file://{path.resolve()}",
            sha256=digest,
            size_bytes=len(data),
            original_tokens=self.counter.count(content),
            summary=self._summary(content),
            kind=kind,
        )

    def resolve(self, pointer: ArtifactPointer, verify: bool = True) -> str:
        """Re-read spilled content; optionally verify the content hash."""
        path = pointer.uri[len("file://"):] if pointer.uri.startswith("file://") \
            else pointer.uri
        data = Path(path).read_bytes()
        if verify and hashlib.sha256(data).hexdigest() != pointer.sha256:
            raise ValueError(
                f"artifact hash mismatch for {pointer.uri}: content on disk "
                f"does not match recorded sha256={pointer.sha256}")
        return data.decode("utf-8")
