"""
Adapter between AgentFlow's existing (synchronous) AgentBase providers and
review_engine.ReviewAgent (async).

Two things this has to get right, both non-obvious from the interfaces alone:

1. AgentBase.send()/._raw_send() are fully synchronous — plain blocking
   HTTP calls (or a subprocess, for CLIAgent). Wrapping a sync call in
   `async def` and `await`-ing it inside `asyncio.gather` does NOT give you
   real concurrency: the coroutine never yields, so N "concurrent" reviews
   just run back-to-back on the event loop thread, taking the same wall time
   as calling them one at a time. We use `asyncio.to_thread` to actually push
   each blocking call onto a worker thread, which is what makes the
   independent-review step actually faster than sequential NEXT_AGENT
   summoning, not just structurally different.

2. Review calls must NOT go through an agent's real session. `AgentBase`
   accumulates `self.history` and tracks token/cost totals on the instance;
   sending review prompts through the same ClaudeAgent instance used in the
   main debate would pollute its conversation history (visible to the
   *next* real debate turn) and mix review-phase cost into debate-phase
   cost accounting. So each scoped review gets a fresh, throwaway AgentBase
   instance built from the same AgentConfig/persona — same model, same
   provider, isolated session — and it's discarded after one call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agents.base import AgentBase, AgentConfig
from agents.providers import create_agent
from review_engine import ReviewAgent


# Maps a persona's declared role (as used in AgentFlow's SPECIALIZED_PERSONAS/
# ROLE_NEEDS) to the review checklist lens defined in review_engine.py.
# Extend this as more personas are added — anything not mapped here falls
# back to a generic lens with no fixed checklist, which review_engine.py
# already handles.
PERSONA_TO_LENS = {
    "security_auditor": "security",
    "ux_simplifier": "ux",
    "cloud_architect": "cloud_deployment",
    "data_architect": "data",
    "devops_engineer": "ops",
}


@dataclass
class AgentBaseReviewer(ReviewAgent):
    """One ephemeral reviewer, built from an existing persona's AgentConfig.

    Not constructed from a live AgentBase instance on purpose — see module
    docstring point 2. Call `for_persona(...)` rather than the constructor
    directly in orchestrator code.
    """

    name: str
    lens: str
    _base_config: AgentConfig
    _review_system_prompt: str

    @staticmethod
    def for_persona(base_config: AgentConfig, persona_role: str, review_system_prompt: str) -> "AgentBaseReviewer":
        lens = PERSONA_TO_LENS.get(persona_role, persona_role)
        return AgentBaseReviewer(
            name=base_config.name,
            lens=lens,
            _base_config=base_config,
            _review_system_prompt=review_system_prompt,
        )

    async def review(self, prompt: str) -> str:
        # Fresh instance per call — cheap (no network cost until .send()),
        # and guarantees no cross-contamination between this review and
        # whatever the same persona is doing in the main debate concurrently.
        ephemeral = create_agent(self._base_config)

        def _blocking_call() -> str:
            return ephemeral.send(prompt, system_override=self._review_system_prompt)

        return await asyncio.to_thread(_blocking_call)


async def build_reviewers_for_component(
    persona_configs: dict[str, AgentConfig],
    relevant_personas: list[str],
) -> list[AgentBaseReviewer]:
    """`persona_configs` keyed by persona role (from the orchestrator's live
    roster). `relevant_personas` should already be filtered by whichever
    lens-relevance rule decides who's allowed to review/contest a given
    component (see design discussion — not every persona should weigh in on
    every decision)."""
    reviewers = []
    for role in relevant_personas:
        config = persona_configs.get(role)
        if config is None:
            continue
        system_prompt = (
            f"{config.system_prompt}\n\n"
            f"You are reviewing an existing design decision as a {role}, "
            f"focused only on your lens. Do not propose changes outside your area."
        )
        reviewers.append(AgentBaseReviewer.for_persona(config, role, system_prompt))
    return reviewers
