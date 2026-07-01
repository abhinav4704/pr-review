"""Phase 3 (vector leg) — embed the semantic identity doc per node.

We embed the *summary, not the code* (ARCHITECTURE.md §7): one short doc per
node composed from its identity + concepts + keywords + signature. The vector
lands as a property on the existing node and is indexed by Neo4j's native vector
index, so the retrieval layer can run cosine search with
`db.index.vector.queryNodes('code_embedding', k, $q)` and then expand along edges.

Provider is pluggable but defaults to a LOCAL sentence-transformers model — no
API key, no per-token cost, fully offline:
    GRAPH_RAG_EMBED_PROVIDER = local | voyage | openai   (default: local)
    GRAPH_RAG_EMBED_MODEL    = model id                  (default: per provider)

SDKs are imported lazily so the rest of the pipeline runs without them installed;
the import error only surfaces when an embedding pass actually runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

EMBED_PROVIDERS = ("local", "voyage", "openai")
DEFAULT_EMBED_PROVIDER = os.environ.get("GRAPH_RAG_EMBED_PROVIDER", "local")

# Bump when the embedding doc composition changes, so a later pass re-embeds.
EMBED_VERSION = "v1"

_DEFAULT_EMBED_MODELS = {
    "local": "all-MiniLM-L6-v2",          # 384-dim, small + fast, no API
    "voyage": "voyage-3",                  # needs VOYAGE_API_KEY + voyageai
    "openai": "text-embedding-3-small",    # needs OPENAI_API_KEY + openai
}

_EMBED_BATCH = 256  # docs per encode/write chunk


def default_embed_model(provider: str) -> str:
    return os.environ.get("GRAPH_RAG_EMBED_MODEL") or _DEFAULT_EMBED_MODELS.get(provider, "")


# --- the embedding doc (summary, not code) ------------------------------------

def embedding_doc(row: dict) -> str:
    """Compose the text we embed for one node, from its identity fields."""
    head = f"[{row.get('fqn') or row.get('name') or ''}] {row.get('identity') or ''}".strip()
    parts = [head]
    concepts = [c for c in (row.get("identity_concepts") or []) if c]
    keywords = [k for k in (row.get("identity_keywords") or []) if k]
    if concepts:
        parts.append("Concepts: " + ", ".join(concepts))
    if keywords:
        parts.append("Keywords: " + ", ".join(keywords))
    sig = row.get("signature")
    if sig:
        parts.append("Signature: " + sig)
    return "\n".join(parts)


# --- provider-agnostic embedder ----------------------------------------------

@dataclass
class Embedder:
    provider: str = DEFAULT_EMBED_PROVIDER
    model: str = ""
    _impl: object = field(default=None, repr=False)
    _dim: int = 0

    def __post_init__(self):
        if self.provider not in EMBED_PROVIDERS:
            raise ValueError(f"unknown embed provider {self.provider!r}; valid: {list(EMBED_PROVIDERS)}")
        if not self.model:
            self.model = default_embed_model(self.provider)

    @property
    def dim(self) -> int:
        if not self._dim:
            self._build()
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text. Raises on transport/import failure."""
        if not texts:
            return []
        self._build()
        if self.provider == "local":
            vecs = self._impl.encode(texts, normalize_embeddings=True, batch_size=32)
            return [list(map(float, v)) for v in vecs]
        if self.provider == "voyage":
            resp = self._impl.embed(texts, model=self.model, input_type="document")
            return [list(map(float, e)) for e in resp.embeddings]
        # openai
        resp = self._impl.embeddings.create(model=self.model, input=texts)
        return [list(map(float, d.embedding)) for d in resp.data]

    # --- lazy provider clients ------------------------------------------------
    def _build(self) -> None:
        if self._impl is not None:
            return
        if self.provider == "local":
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "local embeddings need: pip install sentence-transformers"
                ) from e
            self._impl = SentenceTransformer(self.model)
            self._dim = int(self._impl.get_sentence_embedding_dimension())
        elif self.provider == "voyage":
            try:
                import voyageai  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("voyage embeddings need: pip install voyageai") from e
            if not os.environ.get("VOYAGE_API_KEY"):
                raise RuntimeError("set VOYAGE_API_KEY for the voyage embed provider")
            self._impl = voyageai.Client()
            self._dim = len(self.embed(["probe"])[0])
        else:  # openai
            try:
                from openai import OpenAI  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("openai embeddings need: pip install openai") from e
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("set OPENAI_API_KEY for the openai embed provider")
            self._impl = OpenAI()
            self._dim = len(self.embed(["probe"])[0])


@dataclass
class EmbedResult:
    repo: str
    total: int = 0       # nodes needing an embedding
    embedded: int = 0    # nodes embedded this run
    dim: int = 0


# Nodes with an identity whose vector is missing or stale (identity text changed,
# model changed, or the doc-composition version bumped).
_Q_EMBED_TARGETS = """
MATCH (n:CodeNode {repo:$repo})
WHERE n.identity IS NOT NULL AND (
    $refresh OR n.embedding IS NULL
    OR n.embedding_hash <> coalesce(n.semantic_hash, '')
    OR n.embedding_model <> $model
    OR n.embedding_version <> $version
)
RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.signature AS signature,
       n.identity AS identity, n.identity_concepts AS identity_concepts,
       n.identity_keywords AS identity_keywords, n.semantic_hash AS semantic_hash
"""


def embed_nodes(repo: str, store, embedder: Embedder, limit: int | None = None,
                refresh: bool = False, log=print) -> EmbedResult:
    """Embed every identity-bearing node that needs it, and index the vectors.

    Cached: a node is skipped when its `embedding_hash` already matches its
    current `semantic_hash` (identity text unchanged) and the model/version match.
    """
    rows = store.read(_Q_EMBED_TARGETS, repo=repo, refresh=refresh,
                      model=embedder.model, version=EMBED_VERSION)
    if limit is not None:
        rows = rows[:limit]
    res = EmbedResult(repo=repo, total=len(rows), dim=embedder.dim)
    if not rows:
        log("  nothing to embed (all identities already vectorized)")
        return res

    # Index must exist before writes; dimension is known once the model is loaded.
    store.create_vector_index(embedder.dim)

    for i in range(0, len(rows), _EMBED_BATCH):
        chunk = rows[i:i + _EMBED_BATCH]
        vectors = embedder.embed([embedding_doc(r) for r in chunk])
        out = [{
            "id": r["id"],
            "props": {
                "embedding": vec,
                "embedding_model": embedder.model,
                "embedding_provider": embedder.provider,
                "embedding_dim": embedder.dim,
                "embedding_version": EMBED_VERSION,
                "embedding_hash": r.get("semantic_hash") or "",
            },
        } for r, vec in zip(chunk, vectors)]
        store.write_semantics(out)
        res.embedded += len(out)
        log(f"  embedded {res.embedded}/{res.total}")
    return res
