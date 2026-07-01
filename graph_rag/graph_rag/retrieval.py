"""Phase 3 (retrieval) — hybrid retrieval + LLM prune/answer loop.

This is the loop designed in IMPROVEMENTS.md §1. It stands on primitives that
already exist: the local query embedder (`embeddings.Embedder`), the vector
index (`code_embedding`), the identity keyword/tag/concept fields, the graph
edges, and the structured-output LLM (`llm.SemanticLLM`).

    question
      ├─ embed query (local, no key) ─► vector search (code_embedding)  ┐
      └─ keyword match (keywords/tags/concepts/name/fqn)                ┘─► fuse (RRF)
                                                                              │
                              PRUNE #1 (LLM): drop candidates out of scope    │  ← skipped if no LLM
                                                                              │
                              EXPAND: pull neighbors (callers/callees,        │
                              parent, reads/writes) + their identities        │
                                                                              │
                              PRUNE #2 (LLM): drop irrelevant neighbors       │  ← skipped if no LLM
                                                                              │
                              CONTEXT PACK: targets → full source;            │
                              neighbors → signature + identity (token win)    │
                                                                              │
                              ANSWER (LLM): grounded answer + citations       │  ← skipped if no LLM

Every stage is recorded in `AskResult.stages` and streamed to `log` so you can
watch exactly what was fetched, ranked, pruned, and expanded — which is the
whole point until the answer leg is trustworthy.

Graceful degradation (memory: prune/dedup is LLM-driven, never Python set-logic):
when no `llm` is supplied (e.g. no API/Bedrock creds yet) the two prune passes
and the final answer are skipped — retrieval stops after expansion and returns
the ranked structure for display. With an `llm` the full loop runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .embeddings import Embedder
from .llm import SemanticLLM
from .semantic import _read_source
from .store import GraphStore

# reciprocal-rank-fusion constant; larger = flatter fusion (standard default 60)
_RRF_K = 60

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "how", "what", "where", "which", "does", "do", "did", "when", "why", "who",
    "this", "that", "it", "be", "with", "by", "from", "as", "at", "we", "i",
}


# --- trace ---------------------------------------------------------------------

@dataclass
class Stage:
    """One step of the loop, both logged and returned to the frontend."""
    name: str
    items: list[dict] = field(default_factory=list)
    note: str = ""


@dataclass
class AskResult:
    question: str
    repo: str
    stages: list[Stage] = field(default_factory=list)
    answer: str | None = None
    citations: list[dict] = field(default_factory=list)
    context_chars: int = 0

    def stage(self, name: str) -> Stage | None:
        return next((s for s in self.stages if s.name == name), None)


# --- query prep ----------------------------------------------------------------

def _terms(question: str) -> list[str]:
    """Content tokens from the question, lowercased, stopwords/short dropped."""
    toks = re.findall(r"[A-Za-z0-9_]+", question.lower())
    out: list[str] = []
    for t in toks:
        if len(t) < 3 or t in _STOPWORDS:
            continue
        if t not in out:
            out.append(t)
    return out


# --- candidate legs ------------------------------------------------------------

_Q_VECTOR = """
CALL db.index.vector.queryNodes('code_embedding', $k, $vec) YIELD node, score
WHERE node.repo = $repo
RETURN node.id AS id, node.name AS name, node.fqn AS fqn,
       node.identity AS identity, node.signature AS signature,
       node.file AS file, node.start_line AS start_line, node.end_line AS end_line,
       labels(node) AS labels, score
"""

_Q_KEYWORD = """
MATCH (n:CodeNode {repo:$repo})
WHERE n.identity IS NOT NULL
WITH n, [t IN $terms WHERE
      any(k IN coalesce(n.identity_keywords, []) WHERE toLower(k) CONTAINS t)
   OR any(k IN coalesce(n.identity_concepts, []) WHERE toLower(k) CONTAINS t)
   OR any(k IN coalesce(n.identity_tags,     []) WHERE toLower(k) CONTAINS t)
   OR toLower(coalesce(n.name, '')) CONTAINS t
   OR toLower(coalesce(n.fqn,  '')) CONTAINS t
] AS matched
WITH n, size(matched) AS score, matched WHERE score > 0
RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.identity AS identity,
       n.signature AS signature, n.file AS file, n.start_line AS start_line,
       n.end_line AS end_line, labels(n) AS labels, score, matched
ORDER BY score DESC LIMIT $k
"""


def _label_of(labels) -> str:
    return next((l for l in (labels or []) if l != "CodeNode"), "CodeNode")


def _vector_hits(store: GraphStore, repo: str, vec: list[float], k: int) -> list[dict]:
    # over-fetch: queryNodes returns global top-k, then we filter to this repo.
    rows = store.read(_Q_VECTOR, k=k * 4, vec=vec, repo=repo)
    for r in rows:
        r["label"] = _label_of(r.pop("labels"))
    return rows[:k]


def _keyword_hits(store: GraphStore, repo: str, terms: list[str], k: int) -> list[dict]:
    if not terms:
        return []
    rows = store.read(_Q_KEYWORD, repo=repo, terms=terms, k=k)
    for r in rows:
        r["label"] = _label_of(r.pop("labels"))
    return rows


def _fuse(vector: list[dict], keyword: list[dict], k: int) -> list[dict]:
    """Reciprocal Rank Fusion of the two ranked legs. Scale-free, no tuning:
    a node's score is Σ 1/(RRF_K + rank) over the lists it appears in."""
    agg: dict[str, dict] = {}
    for leg, rows in (("vector", vector), ("keyword", keyword)):
        for rank, r in enumerate(rows):
            node = agg.setdefault(r["id"], {**r, "rrf": 0.0, "legs": []})
            node["rrf"] += 1.0 / (_RRF_K + rank)
            node["legs"].append(f"{leg}#{rank + 1}")
    fused = sorted(agg.values(), key=lambda r: r["rrf"], reverse=True)
    return fused[:k]


# --- LLM prune (token-mapped so the model never echoes raw ids) ----------------

_PRUNE_SYSTEM = (
    "You are pruning code-search candidates for a specific question. Keep only the "
    "entities that are genuinely relevant to answering it; drop the rest. Judge by "
    "the identity/purpose, not keyword overlap. Return the tokens to KEEP.\n"
    "Return only valid JSON matching the supplied schema."
)

_PRUNE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["keep", "reason"],
    "additionalProperties": False,
}


def _prune(llm: SemanticLLM, question: str, rows: list[dict], stage: str, log) -> tuple[list[dict], str]:
    """LLM keeps a subset of `rows`. Returns (kept_rows, reason). On any failure
    it keeps everything (fail-open) so a bad prune never silently drops context."""
    if not rows:
        return rows, "nothing to prune"
    tok = {f"c{i + 1}": r for i, r in enumerate(rows)}
    lines = "\n".join(
        f"[{t}] {r.get('fqn') or r.get('name')} ({r.get('label')}): "
        f"{(r.get('identity') or '(no identity)')}"
        for t, r in tok.items()
    )
    user = (
        f"Question:\n{question}\n\n"
        f"Candidates:\n{lines}\n\n"
        "Return the tokens (e.g. \"c1\") of the candidates to KEEP, plus a one-line reason."
    )
    try:
        data = llm.extract(_PRUNE_SYSTEM, user, _PRUNE_SCHEMA)
    except Exception as e:  # fail open — keep all, surface the error in the log
        log(f"  ! {stage} prune failed ({e}); keeping all {len(rows)}")
        return rows, f"prune skipped (error: {e})"
    keep_toks = {str(t) for t in (data.get("keep") or [])}
    kept = [tok[t] for t in tok if t in keep_toks] or rows  # never prune to empty
    reason = (data.get("reason") or "").strip()
    return kept, reason


# --- graph expansion -----------------------------------------------------------

_Q_EXPAND = """
MATCH (n:CodeNode {repo:$repo}) WHERE n.id IN $ids
CALL {
    WITH n
    MATCH (n)-[r:CALLS|READS|WRITES|CALLS_API|RETURNS|EXTENDS|IMPLEMENTS|OVERRIDES]->(m:CodeNode)
    RETURN m AS nb, type(r) AS rel, 'out' AS dir
  UNION
    WITH n
    MATCH (m:Function)-[r:CALLS]->(n)
    RETURN m AS nb, type(r) AS rel, 'in' AS dir
  UNION
    WITH n
    MATCH (parent:CodeNode)-[:CONTAINS]->(n)
    WHERE parent:Class OR parent:Package
    RETURN parent AS nb, 'CONTAINS' AS rel, 'in' AS dir
}
WITH DISTINCT nb, rel, dir WHERE NOT nb.id IN $ids
RETURN nb.id AS id, nb.name AS name, nb.fqn AS fqn, nb.identity AS identity,
       nb.signature AS signature, labels(nb) AS labels, rel, dir
LIMIT $limit
"""


def _expand(store: GraphStore, repo: str, ids: list[str], limit: int) -> list[dict]:
    if not ids:
        return []
    rows = store.read(_Q_EXPAND, repo=repo, ids=ids, limit=limit)
    for r in rows:
        r["label"] = _label_of(r.pop("labels"))
    return rows


# --- context pack + answer -----------------------------------------------------

_ANSWER_SYSTEM = (
    "You answer questions about a codebase using ONLY the provided context "
    "(retrieved source + neighbor identities). Ground every claim in that context; "
    "if it is insufficient, say so rather than inventing. Cite the entities you "
    "used by their [token].\n"
    "Return only valid JSON matching the supplied schema."
)

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "citations", "confidence"],
    "additionalProperties": False,
}


def _context_pack(root: str, targets: list[dict], neighbors: list[dict]) -> tuple[str, dict]:
    """Target nodes get full source; neighbors get signature + identity only —
    that asymmetry is the token win. Returns (prompt_text, token->row map)."""
    tok: dict[str, dict] = {}
    parts: list[str] = ["=== PRIMARY (retrieved) ==="]
    for i, r in enumerate(targets):
        t = f"c{i + 1}"
        tok[t] = r
        src = _read_source(root, r.get("file"), r.get("start_line") or 0, r.get("end_line") or 0)
        parts.append(f"\n[{t}] {r.get('fqn') or r.get('name')} ({r.get('label')})")
        if r.get("identity"):
            parts.append(f"identity: {r['identity']}")
        if src:
            parts.append("source:\n" + src)
        elif r.get("signature"):
            parts.append("signature: " + r["signature"])
    if neighbors:
        parts.append("\n=== NEIGHBORS (signature + identity only) ===")
        for j, r in enumerate(neighbors):
            t = f"n{j + 1}"
            tok[t] = r
            rel = f"{r.get('dir', '')}:{r.get('rel', '')}".strip(":")
            parts.append(
                f"\n[{t}] {r.get('fqn') or r.get('name')} ({r.get('label')}, {rel})"
                + (f"\nsignature: {r['signature']}" if r.get("signature") else "")
                + (f"\nidentity: {r['identity']}" if r.get("identity") else "")
            )
    return "\n".join(parts), tok


def _answer(llm: SemanticLLM, question: str, pack: str, tok: dict) -> tuple[str, list[dict], float]:
    user = f"Question:\n{question}\n\nContext:\n{pack}\n\nAnswer the question and cite the [tokens] you used."
    data = llm.extract(_ANSWER_SYSTEM, user, _ANSWER_SCHEMA)
    cited = []
    for t in data.get("citations") or []:
        r = tok.get(str(t))
        if r and r["id"] not in {c["id"] for c in cited}:
            cited.append({"id": r["id"], "fqn": r.get("fqn") or r.get("name"), "label": r.get("label")})
    return (data.get("answer") or "").strip(), cited, float(data.get("confidence") or 0.0)


# --- orchestrator --------------------------------------------------------------

def ask(question: str, repo: str, root: str, store: GraphStore, embedder: Embedder,
        llm: SemanticLLM | None = None, *, top_k: int = 8, expand_top: int = 5,
        neighbor_limit: int = 40, log=print) -> AskResult:
    """Run the hybrid retrieval loop; return a fully-traced AskResult.

    With `llm` the full loop runs (prune #1 → expand → prune #2 → answer). Without
    it, retrieval stops after expansion and returns the ranked structure — no
    prune, no answer (the deterministic legs still need embeddings/identities,
    which are produced by the LLM-driven `semantic`/`embed` passes).
    """
    res = AskResult(question=question, repo=repo)

    def _brief(rows):
        return [{"id": r["id"], "fqn": r.get("fqn") or r.get("name"), "label": r.get("label"),
                 "identity": r.get("identity"), "score": round(r.get("rrf", r.get("score", 0.0)), 4),
                 "legs": r.get("legs"), "rel": r.get("rel"), "dir": r.get("dir")}
                for r in rows]

    # 1) candidates — two legs
    terms = _terms(question)
    log(f"\n  question: {question}")
    log(f"  terms:    {terms}")
    vec = embedder.embed([question])[0]
    vrows = _vector_hits(store, repo, vec, top_k)
    krows = _keyword_hits(store, repo, terms, top_k)
    log(f"\n  [candidates] vector={len(vrows)}  keyword={len(krows)}")
    for r in vrows:
        log(f"    vec  {r.get('score', 0):.3f}  {r.get('fqn') or r.get('name')}")
    for r in krows:
        log(f"    kw   {r.get('score', 0)}      {r.get('fqn') or r.get('name')}")
    res.stages.append(Stage("candidates", _brief(vrows) + _brief(krows),
                            note=f"vector={len(vrows)} keyword={len(krows)}"))

    # 2) fuse / rerank
    fused = _fuse(vrows, krows, top_k)
    log(f"\n  [rerank] fused top {len(fused)} (RRF):")
    for r in fused:
        log(f"    {r['rrf']:.4f}  {r.get('fqn') or r.get('name')}  [{','.join(r['legs'])}]")
    res.stages.append(Stage("rerank", _brief(fused), note="reciprocal rank fusion"))

    if not fused:
        log("\n  no candidates — is this repo indexed + enriched (semantic/embed)?")
        return res

    # 3) prune #1 (LLM only)
    if llm is not None:
        kept, reason = _prune(llm, question, fused, "candidate", log)
        log(f"\n  [prune #1] kept {len(kept)}/{len(fused)} — {reason}")
        res.stages.append(Stage("prune_candidates", _brief(kept), note=reason))
        targets = kept
    else:
        targets = fused[:expand_top]
        log(f"\n  [prune #1] skipped (no LLM) — taking top {len(targets)} of rerank")
        res.stages.append(Stage("prune_candidates", _brief(targets),
                                note="skipped (no LLM); took top of rerank"))

    # 4) graph expansion — neighbors of the kept/top targets
    target_ids = [r["id"] for r in targets[:expand_top]]
    neighbors = _expand(store, repo, target_ids, neighbor_limit)
    log(f"\n  [expand] {len(neighbors)} neighbors of {len(target_ids)} nodes:")
    for r in neighbors:
        log(f"    {r['dir']:>3}:{r['rel']:<10} {r.get('fqn') or r.get('name')}")
    res.stages.append(Stage("expand", _brief(neighbors), note=f"{len(neighbors)} neighbors"))

    # 5) prune #2 (LLM only)
    if llm is not None and neighbors:
        neighbors, reason2 = _prune(llm, question, neighbors, "neighbor", log)
        log(f"\n  [prune #2] kept {len(neighbors)} neighbors — {reason2}")
        res.stages.append(Stage("prune_neighbors", _brief(neighbors), note=reason2))

    # 6) context pack
    pack, tok = _context_pack(root, targets[:expand_top], neighbors)
    res.context_chars = len(pack)
    log(f"\n  [context] {len(pack)} chars  ({len(targets[:expand_top])} primary + {len(neighbors)} neighbors)")

    # 7) answer (LLM only)
    if llm is not None:
        try:
            answer, citations, conf = _answer(llm, question, pack, tok)
            res.answer, res.citations = answer, citations
            log(f"\n  [answer] (confidence {conf:.2f})\n{answer}\n")
            log(f"  cites: {[c['fqn'] for c in citations]}")
            res.stages.append(Stage("answer", citations, note=f"confidence={conf:.2f}"))
        except Exception as e:
            log(f"\n  ! answer generation failed: {e}")
            res.stages.append(Stage("answer", [], note=f"failed: {e}"))
    else:
        log("\n  [answer] skipped (no LLM) — showing retrieved context only")

    return res
