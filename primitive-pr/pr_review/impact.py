"""Impact-chain tracing: how a PR's change propagates to the code that breaks.

The per-node review in ``pr_passes`` only ever looks one hop out from each
changed function (its direct callers, its direct callees), and each changed
function is processed on its own. That loses the *connection between* changed
functions. If ``f1`` is a schema, ``f2`` returns that schema, and ``g1`` (which
the PR did NOT touch) consumes ``f2``'s result, the chain ``f1 -> f2 -> g1`` is
never assembled, so the breakage in ``g1`` is invisible.

This module fixes that by working on the whole PR at once:

  1. build_change_clusters — group changed functions that are directly call-
     related into one unit ({f1, f2}). This is the missing interconnection.
  2. impact_tree — a single multi-source reverse-BFS from a cluster, walking the
     caller direction, *keeping parent pointers* so every reachable node is
     visited exactly ONCE (no repeated outputs) on one representative shortest
     path. Walk through changed intermediates; the first UNCHANGED node on a path
     is a "frontier consumer" — a breakage candidate.
  3. extract chains + rank — one chain per consumer: source -> ... -> consumer.

It operates purely on the in-memory NetworkX graph (``CodeGraph.g``) so it can
read edge attributes (e.g. ``confidence`` from graph._resolve) and reconstruct
paths — neither of which the Neo4j ``DISTINCT`` neighbour queries can do.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import networkx as nx

from .diff import ChangedNode, FileDiff, map_changes, node_was_modified
from .graph_contract import (
    confidence_score,
    edge_relation,
    is_ambiguous_confidence,
    is_same_file_confidence,
    relation_weight,
)
from .graph import CodeGraph

# Edges that mean "X depends on Y" — followed in REVERSE (in-edges) to find who
# is impacted when Y changes. All four point dependent -> dependency, so the
# in-edges of a changed node are the things that could break.
#   calls        caller     -> callee
#   instantiates constructor -> class
#   overrides    subclass-m  -> parent-method   (parent changes -> override affected)
#   inherits     subclass    -> parent-class    (parent changes -> subclass affected)
IMPACT_EDGE_TYPES = {
    "calls",
    "instantiates",
    "overrides",
    "inherits",
    "imports",
    "references",
    "uses",
    "implements",
    "extends",
    "re_exports",
    "decorates",
}

# Edges used to group co-changed functions into one cluster (either direction).
CLUSTER_EDGE_TYPES = {"calls", "instantiates", "overrides", "inherits"}

# Substrings that mark a sensitive surface — bumps a chain's priority.
SENSITIVE_NAMES = {
    "auth", "password", "passwd", "secret", "token", "credential", "login",
    "payment", "billing", "admin", "permission", "privilege", "session",
    "encrypt", "decrypt", "signature", "verify",
}

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_CONSUMERS = 15

# data-flow heuristic: dict-literal keys and ['key'] subscripts a function produces
_DICT_KEY = re.compile(r"""["']([A-Za-z_]\w*)["']\s*:""")
_SUBSCRIPT_KEY = re.compile(r"""\[\s*["']([A-Za-z_]\w*)["']\s*\]""")


# ── data model ─────────────────────────────────────────────────────────────────
@dataclass
class ChainNode:
    node_id: str
    role: str               # "source" | "intermediate" | "consumer"
    changed: bool
    change_type: Optional[str]   # signature|added|behavior if changed, else None
    modified_in_pr: bool
    kind: str
    qualname: str
    path: str
    start_line: int
    end_line: int
    is_test: bool
    # how the PREVIOUS node in the chain links to this one (empty for the source)
    edge_type: str = ""
    edge_confidence: str = ""


@dataclass
class Chain:
    """source(changed) -> ...intermediates... -> consumer(unchanged)."""
    nodes: List[ChainNode]
    consumer_id: str
    distance: int                  # hops from source to consumer
    score: float = 0.0
    is_entrypoint: bool = False    # consumer is a route/event
    via_tests: bool = False        # consumer is a test
    modified_consumer: bool = False  # consumer was touched in the PR
    # fields the change removed/renamed that this consumer still references
    field_hits: List[str] = field(default_factory=list)
    # a hop in this chain came from a guessed (same-name) edge — treat as a maybe
    uncertain: bool = False

    @property
    def source(self) -> ChainNode:
        return self.nodes[0]

    @property
    def consumer(self) -> ChainNode:
        return self.nodes[-1]


@dataclass
class Cluster:
    members: List[str]                       # changed node ids in this cluster
    chains: List[Chain] = field(default_factory=list)
    extra_consumers: int = 0                 # consumers dropped past the cap ("+N more")


@dataclass
class ImpactResult:
    clusters: List[Cluster] = field(default_factory=list)
    changed_nodes: List[ChangedNode] = field(default_factory=list)


# ── clustering ──────────────────────────────────────────────────────────────────
def build_change_clusters(cg: CodeGraph, changed: Set[str]) -> List[List[str]]:
    """Group changed nodes that are directly call/inherit-related into clusters.

    Two changed functions land in the same cluster if a direct edge of a
    CLUSTER_EDGE_TYPE connects them (in either direction). Connected components
    of that relation are the clusters; an isolated change is its own cluster.
    """
    changed = {c for c in changed if cg.has(c)}
    u = nx.Graph()
    u.add_nodes_from(changed)
    for a, b, data in cg.g.edges(data=True):
        t = edge_relation(data)
        if t in CLUSTER_EDGE_TYPES and a in changed and b in changed:
            u.add_edge(a, b)
    return [sorted(comp) for comp in nx.connected_components(u)]


# ── traversal ───────────────────────────────────────────────────────────────────
def _impactors(cg: CodeGraph, nid: str,
               exclude_ambiguous: bool) -> List[Tuple[str, str, str]]:
    """Nodes that depend on ``nid`` (its in-edges of an impact type).

    Returns (dependent_id, edge_type, confidence). Ambiguous (guessed) edges are
    dropped by default so we don't fabricate chains from same-name collisions.
    """
    out: List[Tuple[str, str, str]] = []
    for dep, _, data in cg.g.in_edges(nid, data=True):
        t = edge_relation(data)
        if t not in IMPACT_EDGE_TYPES:
            continue
        conf = data.get("confidence", "unique")
        if exclude_ambiguous and is_ambiguous_confidence(conf):
            continue
        out.append((dep, t, conf))
    # Deterministic, quality-first ordering: higher confidence + stronger
    # relations first, then stable node-id tie-break.
    out.sort(
        key=lambda x: (
            -confidence_score(x[2]),
            -relation_weight(x[1]),
            str(x[0]),
        )
    )
    return out


def _same_class_prefixes(cluster: List[str]) -> Set[Tuple[str, str]]:
    """Return (file_path, class_name) pairs for all cluster members inside a class.

    Used by impact_tree to decide whether an unchanged same-class sibling (e.g.
    Logger.info when Logger.emit is changed) should be walked through rather than
    treated as a terminal consumer.  Only nodes whose qualname contains a dot are
    considered (method/field inside a class).
    """
    out: Set[Tuple[str, str]] = set()
    for nid in cluster:
        if "::" not in nid:
            continue
        file_part, qualname = nid.split("::", 1)
        if "." in qualname:
            class_name = qualname.split(".")[0]
            out.add((file_part, class_name))
    return out


def _in_same_class(nid: str, prefixes: Set[Tuple[str, str]]) -> bool:
    """True when ``nid`` belongs to one of the (file, class) pairs in ``prefixes``."""
    if "::" not in nid:
        return False
    file_part, qualname = nid.split("::", 1)
    if "." not in qualname:
        return False
    class_name = qualname.split(".")[0]
    return (file_part, class_name) in prefixes


def impact_tree(cg: CodeGraph, cluster: List[str], changed: Set[str],
                max_depth: int = DEFAULT_MAX_DEPTH,
                exclude_ambiguous: bool = True,
                expand_same_class: bool = False,
                ) -> Tuple[Dict[str, Tuple[str, str, str]], Dict[str, int], List[str]]:
    """Multi-source reverse-BFS from a whole cluster.

    Returns (parent, depth, consumers):
      parent[node]   = (parent_node, edge_type, confidence) — one path per node.
      depth[node]    = hops from the nearest cluster member.
      consumers      = unchanged nodes reached (the breakage frontier).

    Each node is visited once (a ``visited`` set), so callers shared by several
    cluster members are reported a single time — this is what removes the
    "repeated outputs" of independent depth-N neighbour queries. We expand only
    CHANGED nodes (cluster members + changed intermediates); an unchanged node is
    terminal — it's a consumer, recorded but not walked past.

    When ``expand_same_class=True``, unchanged nodes that belong to the same
    class (same file + same top-level qualifier) as a cluster member are treated
    as pass-through intermediates rather than terminals.  They are still recorded
    as consumers (so they appear in chains), but the BFS continues through them,
    surfacing the real external callers.  This is necessary for utility classes
    like ``Logger`` where only one private method is changed but every public
    wrapper (``info``, ``warn``, ``error``) is an unchanged sibling.
    """
    same_class: Set[Tuple[str, str]] = _same_class_prefixes(cluster) if expand_same_class else set()

    parent: Dict[str, Tuple[str, str, str]] = {}
    depth: Dict[str, int] = {m: 0 for m in cluster}
    visited: Set[str] = set(cluster)
    consumers: List[str] = []
    q: deque = deque(cluster)

    while q:
        cur = q.popleft()
        if depth[cur] >= max_depth:
            continue
        # Terminal check: an unchanged node is normally terminal unless it belongs
        # to the same class as a cluster member (same-class passthrough).
        is_passthrough = expand_same_class and _in_same_class(cur, same_class)
        if cur not in changed and not is_passthrough:
            continue  # consumer reached earlier — terminal, don't expand
        for dep, etype, conf in _impactors(cg, cur, exclude_ambiguous):
            if dep in visited:
                continue
            visited.add(dep)
            parent[dep] = (cur, etype, conf)
            depth[dep] = depth[cur] + 1
            if dep not in changed:
                consumers.append(dep)
            q.append(dep)
    return parent, depth, consumers


# ── data-flow signal (which produced fields changed, who still uses them) ────────
def _keys_in(lines: List[str]) -> Set[str]:
    keys: Set[str] = set()
    for ln in lines:
        keys.update(_DICT_KEY.findall(ln))
        keys.update(_SUBSCRIPT_KEY.findall(ln))
    return keys


def field_delta(diff_text: str) -> Tuple[Set[str], Set[str]]:
    """(removed_keys, added_keys) from a file's unified diff — dict keys touched."""
    removed, added = [], []
    for ln in (diff_text or "").splitlines():
        if ln.startswith("-") and not ln.startswith("---"):
            removed.append(ln[1:])
        elif ln.startswith("+") and not ln.startswith("+++"):
            added.append(ln[1:])
    return _keys_in(removed), _keys_in(added)


def gone_fields(diff_by_file: Dict[str, str]) -> Set[str]:
    """Keys present in removed lines but not added lines = removed/renamed fields."""
    gone: Set[str] = set()
    for dtext in (diff_by_file or {}).values():
        rem, add = field_delta(dtext)
        gone |= (rem - add)
    return gone


def _references_field(src: str, field_name: str) -> bool:
    f = re.escape(field_name)
    return re.search(rf"""(\[\s*["']{f}["']\s*\])|(\.{f}\b)""", src) is not None


# ── chain construction + ranking ────────────────────────────────────────────────
def _is_sensitive(name: str) -> bool:
    low = (name or "").lower()
    return any(s in low for s in SENSITIVE_NAMES)


def _chain_node(cg: CodeGraph, nid: str, role: str, changed: Set[str],
                ctype: Dict[str, str], modified: bool,
                edge_type: str = "", edge_conf: str = "") -> ChainNode:
    d = cg.node(nid) if cg.has(nid) else {}
    return ChainNode(
        node_id=nid,
        role=role,
        changed=nid in changed,
        change_type=ctype.get(nid),
        modified_in_pr=modified,
        kind=d.get("kind", "?"),
        qualname=d.get("qualname") or d.get("name") or nid,
        path=d.get("path", ""),
        start_line=d.get("start_line", 0),
        end_line=d.get("end_line", 0),
        is_test=bool(d.get("is_test", False)),
        edge_type=edge_type,
        edge_confidence=edge_conf,
    )


def _origin_prefix(cg: CodeGraph, start: str, cluster_set: Set[str],
                   max_len: int = 4) -> List[str]:
    """Deepest changed origin within the cluster that ``start`` (transitively) calls.

    Returns node ids deepest-first, ending at ``start`` — e.g. for
    build_profile→make_user it returns [make_user, build_profile]. Lets the chain
    show the true origin of the change, not just the consumer's direct caller.
    """
    best = [start]

    def dfs(node: str, path: List[str], visited: Set[str]) -> None:
        nonlocal best
        extended = False
        if len(path) < max_len:
            for _, tgt, data in cg.g.out_edges(node, data=True):
                if edge_relation(data) not in ("calls", "instantiates"):
                    continue
                if tgt in cluster_set and tgt not in visited:
                    extended = True
                    dfs(tgt, path + [tgt], visited | {tgt})
        if not extended and len(path) > len(best):
            best = path

    dfs(start, [start], {start})
    best.reverse()       # deepest origin first, ending at start
    return best


def _build_chain(cg: CodeGraph, consumer: str,
                 parent: Dict[str, Tuple[str, str, str]],
                 changed: Set[str], ctype: Dict[str, str],
                 file_diffs: List[FileDiff],
                 gone: Optional[Set[str]] = None,
                 consumer_src_fn: Optional[Callable[[ChainNode], str]] = None,
                 cluster_members: Optional[Set[str]] = None
                 ) -> Chain:
    """Reconstruct source -> ... -> consumer from parent pointers and score it."""
    # walk parent pointers back to the nearest cluster member, collecting edges
    rev: List[Tuple[str, str, str]] = [(consumer, "", "")]
    cur = consumer
    while cur in parent:
        p, etype, conf = parent[cur]
        # the edge (etype) links p -> cur; attach it to cur
        rev[-1] = (rev[-1][0], etype, conf)
        rev.append((p, "", ""))
        cur = p
    rev.reverse()  # now nearest-member-first
    consumer_distance = len(rev) - 1

    nodes: List[ChainNode] = []
    for i, (nid, etype, conf) in enumerate(rev):
        if i == 0:
            role = "source"
        elif i == len(rev) - 1:
            role = "consumer"
        else:
            role = "intermediate"
        modified = node_was_modified(file_diffs, cg, nid)
        nodes.append(_chain_node(cg, nid, role, changed, ctype, modified,
                                 edge_type=etype, edge_conf=conf))

    # prepend the deepest changed origin inside the cluster (display narrative)
    if cluster_members:
        origin_ids = _origin_prefix(cg, nodes[0].node_id, cluster_members)
        if len(origin_ids) > 1:
            prefix: List[ChainNode] = []
            for i, oid in enumerate(origin_ids[:-1]):
                if i == 0:
                    et, cf = "", ""
                else:
                    ed = cg.g.get_edge_data(oid, origin_ids[i - 1]) or {}
                    et, cf = edge_relation(ed), ed.get("confidence", "")
                prefix.append(_chain_node(
                    cg, oid, "source" if i == 0 else "intermediate", changed, ctype,
                    node_was_modified(file_diffs, cg, oid), et, cf))
            # link the (former) source to the nearest origin behind it
            ed = cg.g.get_edge_data(origin_ids[-1], origin_ids[-2]) or {}
            nodes[0].edge_type = edge_relation(ed)
            nodes[0].edge_confidence = ed.get("confidence", "")
            nodes[0].role = "intermediate"
            nodes = prefix + nodes

    consumer_node = nodes[-1]
    chain = Chain(
        nodes=nodes,
        consumer_id=consumer,
        distance=consumer_distance,
        is_entrypoint=consumer_node.kind in ("route", "event"),
        via_tests=consumer_node.is_test,
        modified_consumer=consumer_node.modified_in_pr,
    )
    chain.uncertain = any(is_ambiguous_confidence(n.edge_confidence) for n in nodes)
    # data-flow: does this consumer still reference a removed/renamed field?
    if gone and consumer_src_fn:
        src = consumer_src_fn(consumer_node)
        chain.field_hits = sorted(f for f in gone if _references_field(src, f))
    chain.score = _score_chain(chain)
    return chain


def _score_chain(chain: Chain) -> float:
    """Rank breakage candidates: real, direct, sensitive, entrypoint = higher."""
    score = 0.0
    src = chain.source
    score += {"signature": 3.0, "added": 3.0, "behavior": 1.0}.get(src.change_type, 1.0)
    if chain.field_hits:
        score += 4.0          # consumer still uses a field the change removed/renamed
    if any(_is_sensitive(n.qualname) for n in chain.nodes):
        score += 2.0
    if chain.is_entrypoint:
        score += 3.0
    # closer consumers are more directly impacted / higher confidence
    score += max(0.0, (4 - chain.distance)) * 0.5
    # Reward strong, deterministic path hops over weak structural hops.
    hop_bonus = 0.0
    for node in chain.nodes[1:]:
        hop_bonus += relation_weight(node.edge_type) * confidence_score(node.edge_confidence)
    score += min(hop_bonus, 3.0)
    # a same-file-resolved hop is slightly less certain than a unique one
    if any(is_same_file_confidence(n.edge_confidence) for n in chain.nodes):
        score -= 0.3
    if chain.via_tests:
        score -= 2.0          # a test catching the change is good news, not a break
    if chain.uncertain:
        score -= 2.0          # link is a guess (same-name); rank below sure chains
    if chain.modified_consumer:
        score -= 5.0          # the PR already touched it; author likely handled it
    return score


def analyze_impact(cg: CodeGraph, file_diffs: List[FileDiff],
                   max_depth: int = DEFAULT_MAX_DEPTH,
                   max_consumers_per_cluster: int = DEFAULT_MAX_CONSUMERS,
                   exclude_ambiguous: bool = False,
                   gone: Optional[Set[str]] = None,
                   consumer_src_fn: Optional[Callable[[ChainNode], str]] = None,
                   expand_same_class: bool = False,
                   ) -> ImpactResult:
    """Full pipeline: changed nodes -> clusters -> impact trees -> ranked chains.

    Ambiguous (same-name guessed) edges are INCLUDED by default but flagged on the
    chain (``Chain.uncertain``) and ranked below sure chains — dropping them
    silently hid real callers (e.g. method calls whose name exists on several
    classes). Pass ``exclude_ambiguous=True`` for sure-links-only.

    ``gone`` (removed/renamed field names) + ``consumer_src_fn`` enable the
    data-flow signal: a consumer that still references a removed field is ranked
    higher and flagged on the chain (``Chain.field_hits``).

    ``expand_same_class=True`` passes through unchanged same-class siblings
    (e.g. Logger.info/warn/error when Logger.emit is changed) so external callers
    appear in the impact chains rather than being hidden behind the terminal.
    """
    changed_nodes = map_changes(cg, file_diffs)
    changed: Set[str] = {c.node_id for c in changed_nodes}
    ctype: Dict[str, str] = {c.node_id: c.change_type for c in changed_nodes}

    clusters: List[Cluster] = []
    for members in build_change_clusters(cg, changed):
        member_set = set(members)
        parent, _depth, consumers = impact_tree(
            cg, members, changed, max_depth=max_depth,
            exclude_ambiguous=exclude_ambiguous,
            expand_same_class=expand_same_class,
        )
        chains = [
            _build_chain(cg, c, parent, changed, ctype, file_diffs,
                         gone=gone, consumer_src_fn=consumer_src_fn,
                         cluster_members=member_set)
            for c in consumers
        ]
        chains.sort(key=lambda ch: -ch.score)
        extra = max(0, len(chains) - max_consumers_per_cluster)
        clusters.append(Cluster(
            members=members,
            chains=chains[:max_consumers_per_cluster],
            extra_consumers=extra,
        ))

    # most impactful clusters first
    clusters.sort(key=lambda cl: -(cl.chains[0].score if cl.chains else -1.0))
    return ImpactResult(clusters=clusters, changed_nodes=changed_nodes)


# ── rendering helpers (shared by the LLM dossier and the UI tree) ───────────────
def _short(node: ChainNode) -> str:
    tag = ""
    if node.changed:
        tag = f" [CHANGED:{node.change_type}]"
    elif node.modified_in_pr:
        tag = " [touched]"
    elif node.role == "consumer":
        tag = " [UNCHANGED]"
    extra = ""
    if node.kind in ("route", "event"):
        extra = f" ⟨{node.kind}⟩"
    if node.is_test:
        extra += " ⟨test⟩"
    return f"{node.kind} {node.qualname}{tag}{extra}"


def chain_to_text(chain: Chain) -> str:
    """One-line 'source -> intermediate -> consumer' rendering of a chain."""
    return " → ".join(_short(n) for n in chain.nodes)


def chain_locations(chain: Chain) -> str:
    """Path:line for each hop, for traceability."""
    return " → ".join(
        f"{n.path}:{n.start_line}" if n.path else n.qualname for n in chain.nodes
    )


def cluster_risk(cluster: Cluster) -> Tuple[str, str]:
    """Deterministic risk (label, emoji) from chain signals, before any LLM call.

    High if a consumer still references a removed/renamed field or the top chain
    scores very high; medium for entrypoint consumers or moderate scores.
    """
    if not cluster.chains:
        return ("low", "🟢")
    best = cluster.chains[0].score
    if any(ch.field_hits for ch in cluster.chains) or best >= 7:
        return ("high", "🔴")
    if any(ch.is_entrypoint for ch in cluster.chains) or best >= 4:
        return ("medium", "🟡")
    return ("low", "🟢")
