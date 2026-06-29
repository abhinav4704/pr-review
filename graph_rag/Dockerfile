# Indexer image: Python (graph_rag) + Node (scip-python), with all deps baked in
# so a run never touches a package registry (the per-run install was the fragile
# part behind a proxy). Build once; runs are offline.
FROM node:20-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (cached layer — only re-runs when requirements.txt changes)
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# scip-python pinned to the validated version (NOT @latest)
RUN npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6

# graph_rag source (.dockerignore keeps .venv / node_modules / caches out)
COPY . .

ENV PATH=/opt/venv/bin:$PATH \
    SCIP_PYTHON_BIN=/app/scip_tooling/node_modules/.bin/scip-python \
    PYTHONUNBUFFERED=1

# Override REPO_NAME at runtime; mount the repo to index at /work/repo
ENV REPO_NAME=sail
CMD ["sh", "-c", "python -m graph_rag.cli index /work/repo --repo \"$REPO_NAME\""]
