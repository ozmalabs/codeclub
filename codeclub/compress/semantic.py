"""
retriever.py — Optional semantic retrieval layer for the compression pipeline.

Architecture
------------
The full pipeline without retrieval:
    stub_all_files → compress → send everything to LLM

With retrieval:
    stub_all_files → embed stubs → ChromaDB index
    task_description → embed → similarity search → top-K stubs
    compact + symbol compress the K stubs → send to LLM (smaller, focused context)

This is aider-style repomap budget management, but with:
  - Semantic similarity (ChromaDB) instead of PageRank
  - Optional cross-encoder re-ranking for precision
  - Our compact + symbol compression on top

ChromaDB is OPTIONAL — if not installed or not configured, NullRetriever
returns all stubs (same behaviour as before). The pipeline always works without it.

Paper note (arXiv:2604.00025)
-----------------------------
Smaller models (0.5B–3B) with brevity constraints match or beat large models on
code tasks. With compressed input + brevity output, routing to a smaller model is
valid. This module pairs with brevity.py which wraps prompts with scale-aware
brevity constraints.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import tiktoken

from .tree import SourceMap, stub_functions, Language, _detect_language

_ENC = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_ENC.encode(text))


# ---------------------------------------------------------------------------
# Retriever protocol — any backend must implement this
# ---------------------------------------------------------------------------

@runtime_checkable
class Retriever(Protocol):
    """
    Protocol for semantic stub retrieval.

    Implementations:
      NullRetriever  — returns all stubs up to token budget (no DB needed)
      ChromaRetriever — embeds stubs, queries by semantic similarity
    """

    def index(self, stubs: dict[str, "StubIndex"]) -> None:
        """Index all stubs. Called once per repo/session."""
        ...

    def query(self, task: str, budget_tokens: int) -> list["RetrievedStub"]:
        """
        Return the most relevant stubs for the given task description,
        fitting within budget_tokens total.
        """
        ...


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StubIndex:
    """One indexed stub entry — a single function/method stub."""
    file: str
    name: str
    stub_text: str       # the compressed stub (sig + "...")
    original_start: int  # orig_start line in source file
    original_end: int    # orig_end line in source file
    language: Language
    tokens: int = field(init=False)

    def __post_init__(self):
        self.tokens = _tokens(self.stub_text)

    @property
    def id(self) -> str:
        # Include orig_start so same-named methods in different files are distinct
        return hashlib.md5(f"{self.file}::{self.name}::{self.original_start}".encode()).hexdigest()[:16]


@dataclass
class RetrievedStub:
    stub: StubIndex
    score: float = 1.0  # relevance score (higher = more relevant)


# ---------------------------------------------------------------------------
# Index builder — stubs a repo and builds StubIndex entries
# ---------------------------------------------------------------------------

def build_stub_index(files: dict[str, str]) -> dict[str, list[StubIndex]]:
    """
    Stub all files in the repo and build a per-file index of StubIndex entries.

    Returns {filename: [StubIndex, ...]}
    """
    index: dict[str, list[StubIndex]] = {}
    for filename, code in files.items():
        language = _detect_language(filename)
        try:
            compressed, smap = stub_functions(code, language)
        except Exception:
            continue
        entries: list[StubIndex] = []
        comp_lines = compressed.splitlines(keepends=True)
        for stub in smap.stubs:
            stub_text = "".join(comp_lines[stub.comp_start: stub.comp_end + 1]).strip()
            if not stub_text:
                continue
            entries.append(StubIndex(
                file=filename,
                name=stub.name,
                stub_text=stub_text,
                original_start=stub.orig_start,
                original_end=stub.orig_end,
                language=language,
            ))
        index[filename] = entries
    return index


# ---------------------------------------------------------------------------
# NullRetriever — no DB, returns all stubs ranked by file/name order
# ---------------------------------------------------------------------------

class NullRetriever:
    """
    Fallback retriever that returns all indexed stubs up to token budget.
    No embedding or vector DB required.
    """

    def __init__(self):
        self._stubs: list[StubIndex] = []

    def index(self, stubs: dict[str, list[StubIndex]]) -> None:
        self._stubs = [s for entries in stubs.values() for s in entries]

    def query(self, task: str, budget_tokens: int) -> list[RetrievedStub]:
        result = []
        used = 0
        for stub in self._stubs:
            if used + stub.tokens > budget_tokens:
                continue
            result.append(RetrievedStub(stub=stub, score=1.0))
            used += stub.tokens
        return result


# ---------------------------------------------------------------------------
# ChromaRetriever — semantic search via ChromaDB + optional re-ranker
# ---------------------------------------------------------------------------

class ChromaRetriever:
    """
    Semantic stub retriever backed by ChromaDB.

    Workflow:
      1. index() embeds each stub's text + metadata into a ChromaDB collection.
      2. query() embeds the task description, does approximate nearest-neighbour
         search, optionally re-ranks with a cross-encoder, then fits within budget.

    Re-ranker is optional. Without it, cosine similarity from ChromaDB is used.
    With it, pass any callable (str, str) -> float as `reranker`.

    ChromaDB uses its default all-MiniLM-L6-v2 embedding model — no API key
    needed, runs locally via sentence-transformers.
    """

    def __init__(
        self,
        collection_name: str = "stub_index",
        reranker=None,
        persist_directory: str | None = None,
    ):
        try:
            import chromadb
            if persist_directory:
                self._client = chromadb.PersistentClient(path=persist_directory)
            else:
                self._client = chromadb.EphemeralClient()
            # Delete existing collection so index() always starts fresh
            try:
                self._client.delete_collection(collection_name)
            except Exception:
                pass
            self._collection = self._client.create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except ImportError:
            raise ImportError(
                "chromadb is required for ChromaRetriever. "
                "Install with: uv pip install chromadb"
            )
        self._reranker = reranker
        self._all_stubs: dict[str, StubIndex] = {}

    def index(self, stubs: dict[str, list[StubIndex]]) -> None:
        """Embed all stubs into ChromaDB."""
        all_entries = [s for entries in stubs.values() for s in entries]
        if not all_entries:
            return

        self._all_stubs = {s.id: s for s in all_entries}

        # ChromaDB accepts batched add
        ids = [s.id for s in all_entries]
        documents = [f"{s.file}::{s.name}\n{s.stub_text}" for s in all_entries]
        metadatas = [
            {
                "file": s.file,
                "name": s.name,
                "tokens": s.tokens,
                "orig_start": s.original_start,
                "orig_end": s.original_end,
                "language": s.language,
            }
            for s in all_entries
        ]
        # Batch in chunks of 100 to avoid ChromaDB limits
        for i in range(0, len(ids), 100):
            self._collection.add(
                ids=ids[i: i + 100],
                documents=documents[i: i + 100],
                metadatas=metadatas[i: i + 100],
            )

    def query(self, task: str, budget_tokens: int, n_results: int = 20) -> list[RetrievedStub]:
        """
        Query ChromaDB for the most relevant stubs, re-rank if available,
        then fit within budget_tokens.
        """
        if not self._all_stubs:
            return []

        n = min(n_results, len(self._all_stubs))
        results = self._collection.query(
            query_texts=[task],
            n_results=n,
            include=["distances", "metadatas"],
        )

        ids = results["ids"][0]
        distances = results["distances"][0]

        # Convert cosine distance to similarity score (0=identical, 2=opposite)
        candidates = [
            RetrievedStub(
                stub=self._all_stubs[sid],
                score=1.0 - (dist / 2.0),  # normalise to [0, 1]
            )
            for sid, dist in zip(ids, distances)
            if sid in self._all_stubs
        ]

        # Optional cross-encoder re-ranking
        if self._reranker and candidates:
            for c in candidates:
                c.score = self._reranker(task, c.stub.stub_text)
            candidates.sort(key=lambda c: c.score, reverse=True)

        # Fit within token budget (highest-scoring first), deduplicate by ID
        result = []
        used = 0
        seen_ids: set[str] = set()
        for c in candidates:
            if c.stub.id in seen_ids:
                continue
            if used + c.stub.tokens <= budget_tokens:
                result.append(c)
                used += c.stub.tokens
                seen_ids.add(c.stub.id)

        return result


# ---------------------------------------------------------------------------
# Convenience: build context from retrieval result
# ---------------------------------------------------------------------------

def render_retrieved_context(retrieved: list[RetrievedStub]) -> str:
    """
    Render retrieved stubs as a context block ready to send to the LLM.

    Groups by file for readability.
    """
    by_file: dict[str, list[RetrievedStub]] = {}
    for r in retrieved:
        by_file.setdefault(r.stub.file, []).append(r)

    lines = []
    for filename, stubs in by_file.items():
        lines.append(f"# {filename}")
        for r in stubs:
            lines.append(r.stub.stub_text)
            lines.append("")
    return "\n".join(lines)
