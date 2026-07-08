from graph_rag.store import GraphStore
from graph_rag.config import neo4j_config

s = GraphStore(neo4j_config())
rows = s.read(
    'MATCH (f:Field {repo:$repo, scope:"module"})<-[r:READS|WRITES]-(fn:Function) '
    'RETURN f.name AS name, f.is_lock AS is_lock, type(r) AS rel, fn.name AS fn ORDER BY name, fn',
    repo='live_test',
)
for r in rows:
    print(dict(r))
s.close()
