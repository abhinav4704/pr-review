# Usage Guide

This repo turns a codebase into a **code knowledge graph** and uses that graph to
do **snippet-driven PR review**: you paste a chunk of changed code, the tool finds
the graph nodes that code touches, pulls in the connected code (callers, callees,
routes, tables, auth dependencies), fetches each node's source by its line range,
and (optionally) sends the whole bundle to an LLM for review.

There are two stages, always in this order:

1. **Build the graph** once per codebase (and re-build when the code changes) →
   `build.py` produces `graph.json`.
2. **Review a snippet** against that graph → `review_pr.py`.

---

## 1. Setup

### 1.1 Python environment

```bash
cd pr-review
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
```

What you install depends on which features you use:

| Feature | Needs |
| --- | --- |
| Build a **Python-only** graph | Nothing beyond the standard library |
| Build a graph **with a React/TS frontend** | Node.js + `typescript` available to `node` |
| **`review_pr.py` extraction** (print the bundle) | Standard library only |
| **`review_pr.py --llm`** (LLM review) | Network + `OPENAI_API_KEY`. `pip install python-dotenv` is optional (enables `.env`) |
| **Neo4j / Cypher Q&A** (`ask_graph.py` default mode) | `pip install neo4j` + a running Neo4j |

For everything, you can simply install the full pinned set:

```bash
pip install -r requirements.txt
```

> The **core PR-review path** (build a Python graph + extract a context bundle) is
> pure standard library and needs no third-party packages.

### 1.2 LLM credentials (only for `--llm`)

The LLM layer talks to an OpenAI-compatible Chat Completions endpoint over plain
HTTP (no SDK). Configure via environment variables or a `.env` file in the repo:

```bash
export OPENAI_API_KEY="sk-..."
# optional:
export OPENAI_BASE_URL="https://api.openai.com/v1"   # default
export OPENAI_MODEL="gpt-4o-mini"                     # default
```

`.env` equivalent (auto-loaded if `python-dotenv` is installed):

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

---

## 2. Build the graph

`build.py` statically analyses a repository and writes `graph.json`.

```bash
python build.py REPO_ROOT [options]
```

| Option | Meaning |
| --- | --- |
| `REPO_ROOT` | Path to the codebase to index (positional, required) |
| `--backend SUBDIR` | Sub-folder that is the Python backend root (used to compute dotted module names) |
| `--frontend SUBDIR` | Sub-folder to scan for JS/TS frontend calls. Omit to skip, or pass a non-existent name to force-skip |
| `--out PATH` | Output graph file (default `graph.json`) |
| `--source-map` | **Embed every file's source text into `graph.json`.** Makes the graph self-contained for slicing (larger file) |
| `--neo4j URI` | Also load into Neo4j, e.g. `bolt://localhost:7687` |
| `--user`, `--password`, `--database` | Neo4j credentials |
| `--wipe` | `DETACH DELETE` the Neo4j DB before loading |

### Recommended: build self-contained for review

```bash
python build.py /path/to/your-app --backend backend --source-map --out graph.json
```

`--source-map` means the review tool never has to read the original checkout — the
source travels inside `graph.json`. This is the most reliable option.

### Notes / gotchas

- A file that fails to parse (Python `SyntaxError`) or a frontend file that can't be
  analysed (e.g. `typescript` not installed) is **skipped with a warning** — the
  build never aborts on one bad file.
- Frontend extraction requires Node + `typescript`. If you only care about backend
  review, pass `--frontend __none__` (any non-existent folder) to skip it entirely.
- `build.py` prints node/edge counts by kind/type at the end. Use this as a sanity
  check that the graph populated.

---

## 3. Review a snippet (`review_pr.py`)

This is the main tool for your use case.

```bash
python review_pr.py (--snippet TEXT | --snippet-file PATH) [options]
```

| Option | Meaning |
| --- | --- |
| `--snippet TEXT` | Inline code to review |
| `--snippet-file PATH` | Read the snippet from a file (valid code; strip diff markers first — see [Plain code vs. raw diffs](#plain-code-vs-raw-diffs-important)) |
| `--graph PATH` | Graph to query (default `graph.json`) |
| `--repo PATH` | Repo root used to read source **when the graph has no embedded `source_map`** (default `.`) |
| `--llm` | Run the LLM review and print feedback (needs `OPENAI_API_KEY`). Without it, prints the JSON bundle |
| `--use-llm-entities` | If deterministic name-matching finds **no** seeds, ask the LLM to pick relevant nodes |
| `--hops-depth N` | Force a fixed BFS expansion depth instead of the risk-adaptive default |
| `--token-limit N` | Budget for the assembled context bundle (default `6000`, ≈ 4 chars/token) |

### 3.1 Extraction only (offline, no API key)

Prints the related-code bundle as JSON — useful to see exactly what the LLM would receive:

```bash
python review_pr.py --snippet-file changed.py --graph graph.json --repo /path/to/your-app
```

Output (per node): `id`, `kind`, `name`, `file`, `source` (sliced from
`line_start`..`line_end`), `hops`. Diagnostics go to **stderr**:

```
# seeds (3): [...]
# bundle nodes: 11 (11 with source)
```

### 3.2 Full LLM review

```bash
export OPENAI_API_KEY="sk-..."
python review_pr.py --snippet-file changed.py --graph graph.json \
    --repo /path/to/your-app --llm
```

Prints reviewer feedback (correctness, security/authz, data access, breaking
changes, impact on the related code).

### 3.3 Inline snippet

```bash
python review_pr.py --snippet 'async def create_session(payload, db, user):
    s = ChatSession(user_id=user.id); db.add(s); return s' --llm
```

### What counts as a usable snippet

The tool anchors to **named entities** the snippet references:

- functions/classes it **defines** (strongest signal),
- functions it **calls** (`create_record(...)`, `service.create()`),
- symbols it **imports**,
- route-path **string literals** (`"/records/{id}"`).

A PR hunk almost always defines or calls a named function/class/route that exists
in the graph, so it anchors well. A snippet with no such identifiers (e.g. a pure
config blob) won't match — use `--use-llm-entities` as a fallback.

### Plain code vs. raw diffs (important)

The snippet must be **valid, parseable code** — a normal code block is the ideal
input. The tool is *not* diff-aware: it does not understand unified-diff syntax.

| Input | Works? | Notes |
| --- | --- | --- |
| A normal code snippet (function/class/block) | ✅ Yes | The intended input |
| A raw unified diff (lines starting with `+`, `-`, `@@`) | ❌ No | Those markers aren't valid code, so parsing fails and nothing anchors |
| A diff with the markers stripped | ✅ Yes | Becomes valid code again |

So if you are copying from a PR diff, **strip the diff markers first**, e.g.:

```bash
sed -E 's/^[+-]//; /^@@/d' raw.diff > changed.py
python review_pr.py --snippet-file changed.py --graph graph.json
```

For a proper diff workflow (changed file + line ranges instead of text matching),
the building block already exists: `cpg.locate.seeds_from_changes(file, line_ranges,
nodes)` maps a changed file's line spans to nodes by `line_start`/`line_end`
overlap. It is not yet wired to a CLI flag — that is the planned `--diff` path.

---

## 4. Ad-hoc graph queries (`ask_graph.py`)

Independent of PR review, you can explore the graph directly.

**Offline hop traversal (no LLM, no Neo4j):**

```bash
# from a known node id
python ask_graph.py --hops --seed "func::app/routes.py::login" --graph graph.json
# by name fragment
python ask_graph.py --hops --name "login" --graph graph.json
# list what's in the graph
python ask_graph.py --list-kinds --graph graph.json
```

**LLM → Cypher over Neo4j (needs `OPENAI_API_KEY` + Neo4j):**

```bash
python ask_graph.py "Which routes are guarded by auth?" \
    --neo4j bolt://localhost:7687 --user neo4j --password password
```

---

## 5. End-to-end example

```bash
# 1. Index the app you want to review (self-contained graph)
python build.py ~/work/my-fastapi-app --backend backend --source-map --out graph.json

# 2. Save the changed code from a PR into a file
cat > changed.py <<'EOF'
async def create_chat_session(payload, db, current_user):
    session = ChatSession(user_id=current_user.id)
    db.add(session)
    return session
EOF

# 3a. Inspect what will be sent (offline)
python review_pr.py --snippet-file changed.py --graph graph.json

# 3b. Get the review (LLM)
export OPENAI_API_KEY="sk-..."
python review_pr.py --snippet-file changed.py --graph graph.json --llm
```

---

## 6. Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `No graph nodes matched the snippet.` | The snippet defines/calls nothing the graph indexes. Confirm the graph covers that file (`--list-kinds`); try `--use-llm-entities`. |
| `bundle nodes: N (0 with source)` + `WARNING: ... no source could be read` | The graph was built over a different/absent checkout and has no `source_map`. Rebuild with `--source-map`, or point `--repo` at the indexed source. With `--llm`, the tool **refuses** rather than send empty context. |
| `OPENAI_API_KEY is required ...` | Export the key (or put it in `.env`). |
| `skip (frontend) ...: typescript` during build | Node/`typescript` not installed. Harmless — those files are skipped. Install `typescript` or ignore if backend-only. |
| `Graph file not found` | Build it first with `build.py`. |
| Empty/odd review | Increase `--token-limit`, or `--hops-depth 2`/`3` to widen the context. |

---

## 7. Quick reference

```bash
# Build (self-contained)
python build.py <app> --backend backend --source-map --out graph.json

# Review (offline bundle)
python review_pr.py --snippet-file changed.py --graph graph.json

# Review (LLM)
OPENAI_API_KEY=sk-... python review_pr.py --snippet-file changed.py --graph graph.json --llm

# Explore graph
python ask_graph.py --list-kinds --graph graph.json
python ask_graph.py --hops --name "<fragment>" --graph graph.json
```
