# Next TODOs — Semantic Layer (Phase 2)

> The structural graph is done. This is the plan for the **semantic enrichment** pass
> (Stage 5 / Phase 2 in [`ARCHITECTURE.md`](ARCHITECTURE.md)). **Design doc only — no code yet.**

## The one rule: contract first, not prompt first

The classic GraphRAG mistake is asking the LLM to *"summarize this function."* Output format
drifts every call, so six months later none of it is reliably queryable. That's not flexibility,
it's a future bug.

Instead we **define exactly what knowledge to extract** as a fixed schema, and the LLM fills it.
Narrow, reproducible extraction tasks — never open-ended summaries.

## Only two artifacts

```
Identity              — why does this exist? what role does it play?
Implementation Flow   — how does it achieve its goal?
```

Everything else is derived later. The graph already holds the *facts* (callers, callees, types,
metrics, imports, inheritance); the LLM only adds *meaning*. **Never repeat graph facts** in the
semantic output.

---

## Schemas

### Function — two outputs

**FunctionIdentity** (why it exists; no code, no call list):
```yaml
FunctionIdentity:
  purpose:
  responsibility:
  business_goal:
  domain_concepts:
  collaborators:
  preconditions:
  postconditions:
  side_effects:
  importance:
  confidence:
  keywords:        # added — see "Retrieval boost"
  tags:
  concepts:
```

**ImplementationFlow** (how it works):
```yaml
ImplementationFlow:
  steps:
  inputs:
  outputs:
  decision_points:
  external_interactions:
  failure_modes:
  confidence:
```

### Class — two artifacts

**ClassIdentity:**
```yaml
ClassIdentity:
  role:
  purpose:
  responsibilities:
  owned_state:
  collaborators:
  public_api:
  design_patterns:
  business_concepts:
  importance:
  confidence:
  keywords:
  tags:
  concepts:
```

**Behavior** (classes get this instead of an implementation flow):
```yaml
Behavior:
  workflow:          # ordered: receive → validate → execute → persist → publish ...
  major_operations:
  state_changes:
```

### Package — identity only
```yaml
PackageIdentity:
  purpose:
  responsibilities:
  contains:
  depends_on:
  exposes:
  business_area:
  keywords:
  tags:
  concepts:
```

### Repository — identity only
```yaml
RepositoryIdentity:
  domain:
  purpose:
  architecture_style:
  major_subsystems:
  technology_stack:
  external_systems:
  business_capabilities:
  entry_points:
  important_constraints:
  keywords:
  tags:
  concepts:
```

---

## Retrieval boost — keywords / tags / concepts

Every **Identity** also carries `keywords`, `tags`, `concepts`. This is the differentiator: it
makes the semantic layer directly retrievable (keyword/BM25 + concept filtering alongside vector
search). Example:
```yaml
keywords: [Authentication, JWT, Login, Credentials]
tags:     [security, api, service]
concepts: [User, Session, Token]
```

---

## What the LLM receives (context assembly)

Not just code — assemble from the graph:
```
Raw code
+ Graph facts (callers, callees, types, inheritance, imports)
+ Metrics (loc, cyclomatic, fan-in/out)
+ Reads / Writes
+ Enclosing Package Identity
+ Enclosing Class Identity
```
The graph gives facts; the LLM gives meaning.

> **Note — ordering tension to resolve:** generation is bottom-up (a Class consumes its
> Functions' identities), so when a Function is generated its Class/Package identities don't
> exist yet. Decide per level what enclosing context is actually available: Functions likely get
> only structural/graph facts; Classes get their Function identities; Packages get Class
> identities; Repository gets Package identities. (Listed under open decisions.)

## The generic prompt (one prompt, schema injected per call)

> You are extracting semantic knowledge from source code.
> Do NOT summarize syntax. The graph already contains callers, callees, types, metrics, imports,
> inheritance — do NOT repeat those facts.
> Your task is to extract **meaning**: purpose, responsibility, business concepts, behavior,
> intent. Never invent information not supported by the provided context. Prefer concise, factual
> descriptions. Return only valid JSON matching the supplied schema.

Then inject the relevant schema.

## Generation order (bottom-up — dependencies)

```
Functions → Classes → Packages → Repository
```
- Class Identity depends on its Function Identities.
- Package Identity depends on its Class Identities.
- Repository Identity depends on its Package Identities.

## Keep Identity and Implementation Flow as **separate generations**

Not one combined prompt. They answer different questions, so separate calls produce cleaner,
more stable output and let you **regenerate one without touching the other**. Retrieval uses
them differently: *"what does this service do?"* → identities; *"how is auth implemented?"* →
implementation flows.

## The hard rule

Never ask *"summarize this function."* Always ask *"extract semantic identity"* or *"extract
implementation flow."* Narrow tasks → reproducible output.

---

## Open decisions (to settle before coding)
- **Storage:** identity/flow as JSON properties on the existing node, or separate
  `:Identity`/`:Flow` nodes linked to it? (Leaning: properties + a built embedding doc per node.)
- **Embedding doc:** compose from Identity fields (purpose + concepts + signature) — embed the
  summary, not the code.
- **Caching/freshness:** key generations by `body_hash` so only changed nodes re-generate.
- **Model tiering:** cheap model for routine functions, strong model for class/package/repo
  synthesis; template trivial members (getters/setters/`__init__`) with no LLM call.
- **Provider/model + JSON-schema enforcement** (structured output / tool-use) — TBD.
- **Confidence:** every artifact carries `confidence`; how to set it deterministically vs. let
  the model self-report.
