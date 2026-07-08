"""Minimal deterministic web UI over the code graph.

No LLM anywhere — everything is read straight from Neo4j + the source on disk:
  • add a repo (GitHub clone or .zip upload) → runs the indexing pipeline
  • search functions / classes
  • for any node: metadata, dependencies (in/out edges), and real source code

Run:  NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --reload --port 8000
Then open http://localhost:8000
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from neo4j import GraphDatabase

from graph_rag.graph_core.config import neo4j_config
from graph_rag.graph_core.pipeline import index_repo
from graph_rag.graph_core.store import GraphStore

HERE = os.path.dirname(os.path.abspath(__file__))
REPOS_DIR = os.path.join(HERE, "repos")
REGISTRY = os.path.join(REPOS_DIR, "registry.json")
STATIC = os.path.join(HERE, "static")
os.makedirs(REPOS_DIR, exist_ok=True)

_cfg = neo4j_config()
_driver = GraphDatabase.driver(_cfg.uri, auth=(_cfg.user, _cfg.password))

app = FastAPI(title="Codebase Brain — deterministic browser")


# ----------------------------------------------------------------- registry --
def _load_registry() -> dict:
    if os.path.exists(REGISTRY):
        with open(REGISTRY) as fh:
            return json.load(fh)
    return {}


def _save_registry(reg: dict) -> None:
    with open(REGISTRY, "w") as fh:
        json.dump(reg, fh, indent=2)


def _register(name: str, root: str, source: str) -> None:
    reg = _load_registry()
    reg[name] = {"root": os.path.abspath(root), "source": source,
                 "indexed_at": int(time.time())}
    _save_registry(reg)


def _seed_local():
    """Make any already-indexed local repos browsable (source viewing needs a path)."""
    reg = _load_registry()
    candidate = os.path.abspath(os.path.join(HERE, "..", "..", "primitive-pr"))
    if "primitive-pr" not in reg and os.path.isdir(candidate):
        reg["primitive-pr"] = {"root": candidate, "source": "local",
                               "indexed_at": 0}
        _save_registry(reg)


_seed_local()


# -------------------------------------------------------------------- neo4j --
def q(cypher: str, **params):
    with _driver.session(database=_cfg.database) as s:
        return [r.data() for r in s.run(cypher, parameters=params)]


def _specific_label(labels):
    return next((l for l in labels if l != "CodeNode"), "CodeNode")


# --------------------------------------------------------------- repo mgmt ---
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_name(name: str) -> str:
    name = _SAFE.sub("-", name.strip()) or "repo"
    return name[:64]


def _index_path(root: str, name: str) -> dict:
    store = GraphStore(_cfg)
    try:
        res = index_repo(root, name, store, wipe=True)
    finally:
        store.close()
    _register(name, root, _load_registry().get(name, {}).get("source", "local"))
    return {"repo": name, "files": res.files, "nodes": res.nodes,
            "edges": res.edges, "seconds": round(res.seconds, 2),
            "scip": res.scip.available, "scip_calls": res.scip.calls}


@app.get("/api/repos")
def list_repos():
    reg = _load_registry()
    out = []
    for name, meta in sorted(reg.items()):
        counts = q("MATCH (n:CodeNode {repo:$r}) RETURN count(n) AS n", r=name)
        out.append({"name": name, "source": meta.get("source"),
                    "nodes": counts[0]["n"] if counts else 0,
                    "has_source": bool(meta.get("root") and os.path.isdir(meta["root"]))})
    return out


@app.post("/api/repos/github")
def add_github(url: str = Form(...), name: str = Form("")):
    if not name:
        name = os.path.splitext(os.path.basename(url.rstrip("/")))[0]
    name = _safe_name(name)
    dest = os.path.join(REPOS_DIR, name)
    shutil.rmtree(dest, ignore_errors=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, dest],
                       check=True, capture_output=True, text=True, timeout=300)
    except subprocess.CalledProcessError as e:
        raise HTTPException(400, f"git clone failed: {e.stderr[-400:]}")
    _register(name, dest, "github")
    return _index_path(dest, name)


@app.post("/api/repos/upload")
async def add_upload(file: UploadFile = File(...), name: str = Form("")):
    if not name:
        name = os.path.splitext(file.filename or "repo")[0]
    name = _safe_name(name)
    dest = os.path.join(REPOS_DIR, name)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        zpath = tmp.name
    try:
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a valid .zip file")
    finally:
        os.unlink(zpath)
    # if the zip had a single top-level dir, index that
    entries = [os.path.join(dest, e) for e in os.listdir(dest)]
    root = entries[0] if len(entries) == 1 and os.path.isdir(entries[0]) else dest
    _register(name, root, "upload")
    return _index_path(root, name)


# ------------------------------------------------------------------- nodes ---
@app.get("/api/nodes")
def search_nodes(repo: str, query: str = "", label: str = "", limit: int = 200):
    labels = ["Function", "Class"] if not label else [label]
    rows = q(
        """
        MATCH (n:CodeNode {repo:$repo})
        WHERE any(l IN labels(n) WHERE l IN $labels)
          AND ($query = '' OR toLower(n.name) CONTAINS toLower($query)
               OR toLower(n.fqn) CONTAINS toLower($query))
        RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.kind AS kind,
               n.file AS file, n.start_line AS line, labels(n) AS labels
        ORDER BY n.name LIMIT $limit
        """,
        repo=repo, labels=labels, query=query, limit=limit,
    )
    for r in rows:
        r["label"] = _specific_label(r.pop("labels"))
    return rows


def _read_source(repo: str, relpath: str, start: int, end: int) -> str:
    meta = _load_registry().get(repo)
    if not meta or not meta.get("root") or not relpath:
        return ""
    path = os.path.normpath(os.path.join(meta["root"], relpath))
    if not path.startswith(os.path.abspath(meta["root"])):  # path-traversal guard
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    s = max(1, start or 1)
    e = min(len(lines), end or len(lines))
    return "".join(lines[s - 1:e])


@app.get("/api/nodes/{node_id}")
def node_detail(node_id: str):
    rows = q("MATCH (n:CodeNode {id:$id}) RETURN n, labels(n) AS labels", id=node_id)
    if not rows:
        raise HTTPException(404, "node not found")
    n = rows[0]["n"]
    n["label"] = _specific_label(rows[0]["labels"])

    out = q(
        """MATCH (n:CodeNode {id:$id})-[r]->(m:CodeNode)
           RETURN type(r) AS rel, r.confidence AS confidence, r.origin AS origin,
                  r.extractor AS extractor, r.evidence_file AS ef,
                  r.evidence_line AS el, m.id AS id, m.name AS name,
                  labels(m) AS labels ORDER BY rel, name""", id=node_id)
    inc = q(
        """MATCH (n:CodeNode {id:$id})<-[r]-(m:CodeNode)
           RETURN type(r) AS rel, r.confidence AS confidence, r.origin AS origin,
                  r.extractor AS extractor, r.evidence_file AS ef,
                  r.evidence_line AS el, m.id AS id, m.name AS name,
                  labels(m) AS labels ORDER BY rel, name""", id=node_id)
    for r in out + inc:
        r["label"] = _specific_label(r.pop("labels"))

    source = _read_source(n.get("repo"), n.get("file"),
                          n.get("start_line"), n.get("end_line"))
    return {"node": n, "outgoing": out, "incoming": inc, "source": source}


# --------------------------------------------------------------------- ask ---
@app.post("/api/ask")
def ask_question(repo: str = Form(...), question: str = Form(...),
                 top_k: int = Form(8), expand: int = Form(5), use_llm: bool = Form(False)):
    """Run the hybrid retrieval loop and return the full per-stage trace.

    use_llm=false (default) runs retrieval only — no prune, no answer — so it
    works with just the local embedder. use_llm=true runs the full loop and
    needs LLM creds (ANTHROPIC_API_KEY / Bedrock AWS creds)."""
    from graph_rag.rag.embeddings import DEFAULT_EMBED_PROVIDER, Embedder, default_embed_model
    from graph_rag.graph_core.llm import SemanticLLM
    from graph_rag.rag.retrieval import ask as retrieval_ask

    meta = _load_registry().get(repo) or {}
    root = meta.get("root") or ""
    store = GraphStore(_cfg)
    embedder = Embedder(provider=DEFAULT_EMBED_PROVIDER,
                        model=default_embed_model(DEFAULT_EMBED_PROVIDER))
    llm = SemanticLLM() if use_llm else None
    try:
        res = retrieval_ask(question, repo, root, store, embedder, llm,
                            top_k=top_k, expand_top=expand, log=lambda *_: None)
    except Exception as e:
        raise HTTPException(400, f"retrieval failed: {e}")
    finally:
        store.close()
    return {
        "question": res.question, "repo": res.repo, "answer": res.answer,
        "citations": res.citations, "context_chars": res.context_chars,
        "stages": [{"name": s.name, "note": s.note, "items": s.items} for s in res.stages],
    }


# ------------------------------------------------------------------- static --
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/ask")
def ask_page():
    return FileResponse(os.path.join(STATIC, "ask.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
