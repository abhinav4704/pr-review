"""Local semantic embedding index.

Uses sentence-transformers (all-MiniLM-L6-v2, 80MB, CPU-fast) to embed code
chunks and answer semantic queries like:
  "find code that handles authentication patterns"
  "find similar error handling"
  "what other code follows this convention"

No Qdrant. Embeddings are stored as a numpy float32 matrix in memory.
The index is built once per graph (lazy, on first query) and cached.

If sentence-transformers is not installed, all queries return [].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from .graph import CodeGraph

_MODEL_NAME = "all-MiniLM-L6-v2"
_CHUNK_LINES = 30        # max lines per embedding chunk
_OVERLAP_LINES = 5       # overlap between consecutive chunks
_TOP_K_DEFAULT = 8


@dataclass
class Chunk:
    node_id: str    # graph node this chunk belongs to (or file path for file-level)
    path: str
    start_line: int
    end_line: int
    text: str


@dataclass
class EmbeddingIndex:
    chunks: List[Chunk] = field(default_factory=list)
    matrix: Optional[np.ndarray] = None  # shape (N, D)
    _model: object = field(default=None, repr=False)
    _ready: bool = False

    def _load_model(self) -> bool:
        if self._model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(_MODEL_NAME)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def build(self, cg: CodeGraph) -> None:
        """Embed all definition nodes from the graph."""
        if not self._load_model():
            return
        self.chunks = []
        texts: List[str] = []

        for nid, data in cg.g.nodes(data=True):
            if data.get("kind") == "file":
                continue
            src = cg.source(nid)
            if not src.strip():
                continue
            # split into overlapping chunks if the node is long
            lines = src.splitlines()
            for start_i in range(0, len(lines), _CHUNK_LINES - _OVERLAP_LINES):
                end_i = min(start_i + _CHUNK_LINES, len(lines))
                chunk_text = "\n".join(lines[start_i:end_i])
                abs_start = data["start_line"] + start_i
                abs_end = data["start_line"] + end_i - 1
                chunk = Chunk(
                    node_id=nid,
                    path=data["path"],
                    start_line=abs_start,
                    end_line=abs_end,
                    text=chunk_text,
                )
                self.chunks.append(chunk)
                texts.append(chunk_text)
                if end_i >= len(lines):
                    break

        if not texts:
            return

        embeddings = self._model.encode(texts, batch_size=64, show_progress_bar=False,
                                        convert_to_numpy=True, normalize_embeddings=True)
        self.matrix = embeddings.astype(np.float32)
        self._ready = True

    def query(self, text: str, top_k: int = _TOP_K_DEFAULT,
              path_filter: Optional[str] = None) -> List[Tuple[Chunk, float]]:
        """Return top_k most similar chunks to `text`, optionally filtered to a file."""
        if not self._ready or self.matrix is None:
            return []
        if not self._load_model():
            return []
        q = self._model.encode([text], normalize_embeddings=True,
                                convert_to_numpy=True)[0].astype(np.float32)
        scores = self.matrix @ q  # cosine similarity (both normalised)
        if path_filter:
            for i, chunk in enumerate(self.chunks):
                if chunk.path != path_filter:
                    scores[i] = -1.0
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [(self.chunks[i], float(scores[i]))
                for i in top_idx if scores[i] > 0.1]

    def find_similar_patterns(self, query: str, top_k: int = 6) -> List[str]:
        """Return formatted snippets for use in a review prompt."""
        results = self.query(query, top_k=top_k)
        out: List[str] = []
        for chunk, score in results:
            out.append(
                f"# Similar pattern ({chunk.path}:{chunk.start_line}-{chunk.end_line}, "
                f"score={score:.2f})\n{chunk.text}"
            )
        return out


def build_index(cg: CodeGraph) -> EmbeddingIndex:
    idx = EmbeddingIndex()
    idx.build(cg)
    return idx
