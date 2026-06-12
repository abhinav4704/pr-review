"""Review depth profiles (tiers).

The same pipeline can run at three levels of cost/depth. A ReviewProfile is a
single config object that gates:
  * which specialist agents run,
  * whether cross-file context (callers/callees from OTHER files) is built,
  * whether the breaking-change / caller-compatibility pass runs,
  * whether the embedding index and evidence verifier are used.

    quick    — file-only. Basic checks (errors + exposed secrets) on the changed
               code itself. No cross-file work. Cheapest.
    standard — quick + check the functions that changed against the other files
               that call them (breaking-change detection).
    deep     — everything: full agent set, architecture + security + performance,
               embeddings, and improvement suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class ReviewProfile:
    key: str                     # "quick" | "standard" | "deep"
    label: str                   # human label for the UI
    description: str             # one-line UI helper
    agent_keys: List[str]        # union of both passes — kept for backward compat
    whole_file_agent_keys: List[str]   # Pass 1: agents that see the full file source
    changed_fn_agent_keys: List[str]   # Pass 2: agents that see changed fns + callers/callees
    cross_file: bool             # reserved/unused: file review now always sends the whole file;
                                 # cross-file reasoning lives in the breaking-change pass below
    caller_compat: bool          # run the breaking-change pass (the only cross-file work)
    use_embeddings: bool         # build/use the semantic embedding index
    verify: bool                 # run the evidence verifier pass


QUICK = ReviewProfile(
    key="quick",
    label="Quick — file only",
    description="Basic checks (bugs + exposed secrets) on the changed code only. No cross-file work.",
    agent_keys=["security", "correctness"],
    whole_file_agent_keys=["security"],
    changed_fn_agent_keys=["correctness"],
    cross_file=False,
    caller_compat=False,
    use_embeddings=False,
    verify=False,
)

STANDARD = ReviewProfile(
    key="standard",
    label="Standard — + dependents",
    description="Quick, plus checks whether changed functions break callers in other files.",
    agent_keys=["security", "correctness", "api_contract", "architecture"],
    whole_file_agent_keys=["security", "architecture"],
    changed_fn_agent_keys=["security", "correctness", "api_contract", "architecture"],
    cross_file=True,
    caller_compat=True,
    use_embeddings=False,
    verify=True,
)

DEEP = ReviewProfile(
    key="deep",
    label="Deep — architecture & security",
    description="Everything: full security, performance, architecture review, and suggestions.",
    agent_keys=["security", "correctness", "performance",
                "api_contract", "architecture"],
    whole_file_agent_keys=["security", "architecture", "performance"],
    changed_fn_agent_keys=["security", "correctness", "api_contract", "architecture"],
    cross_file=True,
    caller_compat=True,
    use_embeddings=True,
    verify=True,
)

PROFILES: Dict[str, ReviewProfile] = {
    QUICK.key: QUICK,
    STANDARD.key: STANDARD,
    DEEP.key: DEEP,
}


def build_agents(agent_keys: List[str]) -> list:
    """Instantiate agent objects for the given keys, preserving order/uniqueness."""
    from .agents import AGENT_REGISTRY
    seen = set()
    agents = []
    for key in agent_keys:
        if key in seen:
            continue
        cls = AGENT_REGISTRY.get(key)
        if cls is not None:
            agents.append(cls())
            seen.add(key)
    return agents
