"""
Independent review engine — the "many specialists look at the same design,
in parallel, without seeing each other" mechanism, plus the merge step that
turns their raw findings into graph mutations (gaps -> contests,
counter-proposals -> dissent + contests) without ever mutating a component
a specialist wasn't scoped to.

This sits between the Orchestrator's turn loop and DesignGraphStore:

    Orchestrator._debate_phase (sequential NEXT_AGENT summoning)  <-- old
    Orchestrator._independent_review_phase(component)             <-- new,
        calls run_independent_review() below, then merge_findings(),
        then applies results via DesignGraphStore.

Each specialist call is intentionally a single isolated request — no shared
conversation, no visibility into other specialists' output — to avoid
anchoring (see design discussion). Only genuine, high-impact conflicts come
back out as contests; everything else is auto-applied as a gap or ignored.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from design_graph import ContestType, DesignGraphStore, Dissent


# ─── Agent-facing contract ───────────────────────────────────────────────────
# Any provider (Claude, OpenAI, Kimi, ...) just needs to implement this narrow
# async interface. Real AgentBase subclasses can wrap _raw_send to satisfy it.

class ReviewAgent(Protocol):
    name: str
    lens: str  # e.g. "security", "ux", "cloud_deployment" — used for scoping

    async def review(self, prompt: str) -> str: ...


REVIEW_PROMPT_TEMPLATE = """You are reviewing ONE part of a system design: "{component}".
Your lens: {lens}.

Current decision for this component:
{current_decision}

Resolved facts you can rely on (do not re-litigate these):
{resolved_dependencies}

Checklist to explicitly consider for this lens (skip items your lens doesn't govern):
{checklist}

Respond with ONLY valid JSON, no markdown fences, no commentary, in this exact shape:
{{
  "gaps": [
    {{"concern": "short name", "severity": "high|medium|low", "detail": "what's missing and why it matters"}}
  ],
  "counter_proposals": [
    {{
      "alternative": "what you'd choose instead",
      "rationale": "why",
      "criteria_optimized": ["..."],
      "criteria_traded_off": ["..."],
      "depends_on_unset_constraint": "constraint_key_or_null"
    }}
  ]
}}
If you have nothing to add, return {{"gaps": [], "counter_proposals": []}}.
"""

DEFAULT_CHECKLISTS: dict[str, list[str]] = {
    "security": ["authn/authz", "data at rest encryption", "audit logging", "secrets handling", "input validation"],
    "ux": ["error states", "loading/empty states", "accessibility", "onboarding friction"],
    "cloud_deployment": ["scaling limits", "region/failover", "cost at target scale", "deployment rollback"],
    "data": ["schema normalization", "migration path", "backup/DR", "query performance at scale"],
    "ops": ["observability", "alerting", "on-call runbook", "rollback strategy"],
}


@dataclass
class Gap:
    concern: str
    severity: str
    detail: str
    raised_by: str


@dataclass
class CounterProposal:
    alternative: str
    rationale: str
    criteria_optimized: list[str]
    criteria_traded_off: list[str]
    depends_on_unset_constraint: Optional[str]
    raised_by: str


@dataclass
class ReviewResult:
    agent: str
    gaps: list[Gap] = field(default_factory=list)
    counter_proposals: list[CounterProposal] = field(default_factory=list)
    parse_error: Optional[str] = None


def _extract_json(text: str) -> dict:
    """Tolerant parse: strips ```json fences if a model added them anyway.
    Raises TypeError (not just JSONDecodeError) if the result isn't the
    expected object shape — e.g. a model that returns a bare string or list
    is just as much a "bad response" as invalid JSON, and callers need to
    catch both the same way."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    return data


async def run_independent_review(
    agents: list[ReviewAgent],
    component: str,
    graph: DesignGraphStore,
    timeout_seconds: float = 60.0,
) -> list[ReviewResult]:
    """Dispatch the same scoped context to every agent CONCURRENTLY and in
    isolation. This is the "independent audit before merge" step — agents
    never see each other's output, which is the point."""
    ctx = graph.dependency_closure_context(component)

    async def _one(agent: ReviewAgent) -> ReviewResult:
        checklist = DEFAULT_CHECKLISTS.get(agent.lens, ["(no fixed checklist for this lens)"])
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            component=component,
            lens=agent.lens,
            current_decision=json.dumps(ctx["current_decision"], indent=2),
            resolved_dependencies=json.dumps(ctx["resolved_dependencies"], indent=2),
            checklist="\n".join(f"- {item}" for item in checklist),
        )
        try:
            raw = await asyncio.wait_for(agent.review(prompt), timeout=timeout_seconds)
            data = _extract_json(raw)
            return ReviewResult(
                agent=agent.name,
                gaps=[Gap(raised_by=agent.name, **g) for g in data.get("gaps", [])],
                counter_proposals=[
                    CounterProposal(raised_by=agent.name, **cp) for cp in data.get("counter_proposals", [])
                ],
            )
        except asyncio.TimeoutError:
            return ReviewResult(agent=agent.name, parse_error="timeout")
        except (json.JSONDecodeError, TypeError) as e:
            return ReviewResult(agent=agent.name, parse_error=f"bad response: {e}")

    return await asyncio.gather(*(_one(a) for a in agents))


# ─── Merge ────────────────────────────────────────────────────────────────

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def merge_findings(results: list[ReviewResult]) -> dict[str, Any]:
    """Dedupe gaps, bucket by severity, and classify counter-proposals into
    contest types BEFORE anything touches the graph or the user. This is the
    step that prevents review-fatigue: nothing here is a graph mutation yet,
    it's a plan for what to apply / ask / escalate."""

    seen_concerns: set[str] = set()
    gaps: list[Gap] = []
    for r in results:
        for g in r.gaps:
            key = g.concern.strip().lower()
            if key in seen_concerns:
                continue
            seen_concerns.add(key)
            gaps.append(g)
    gaps.sort(key=lambda g: SEVERITY_ORDER.get(g.severity, 3))

    missing_constraint_contests = []
    judgment_contests = []
    for r in results:
        for cp in r.counter_proposals:
            if cp.depends_on_unset_constraint:
                missing_constraint_contests.append(cp)
            else:
                judgment_contests.append(cp)

    errors = [r.agent for r in results if r.parse_error]

    return {
        "gaps": gaps,
        "missing_constraint_contests": missing_constraint_contests,
        "judgment_contests": judgment_contests,
        "agents_with_errors": errors,
    }


def apply_merge_to_graph(component: str, merged: dict[str, Any], graph: DesignGraphStore) -> dict[str, Any]:
    """Turn a merge result into actual graph state:
      - gaps become contests of type... actually gaps are additive findings,
        not disagreements about the current value, so they're logged as
        contests only if severity is high enough to block; otherwise they're
        returned as "auto-noted" items the caller can add as new sibling
        decisions (e.g. spin up an "audit_logging" component) rather than
        contesting the existing one.
      - missing_constraint contests get raised directly (resolvable by a
        single follow-up question, not a decision card).
      - judgment contests get raised AND get dissent recorded so the eventual
        decision card has both sides' criteria.
    Returns a summary of what still needs the user.
    """
    node = graph.get_active_decision_for_component(component)
    if node is None:
        raise ValueError(f"No active decision for '{component}' to apply findings to")

    needs_user_questions = []
    needs_user_decision_cards = []

    for cp in merged["missing_constraint_contests"]:
        contest = graph.raise_contest(
            node.id, ContestType.MISSING_CONSTRAINT, cp.raised_by,
            question_or_alternative=(
                f"Need to know '{cp.depends_on_unset_constraint}' to evaluate: {cp.alternative}"
            ),
        )
        needs_user_questions.append({
            "contest_id": contest.id,
            "constraint_key": cp.depends_on_unset_constraint,
            "context": cp.rationale,
        })

    for cp in merged["judgment_contests"]:
        graph.add_dissent(node.id, Dissent(
            agent=cp.raised_by, alternative=cp.alternative,
            criteria_optimized=cp.criteria_optimized, criteria_traded_off=cp.criteria_traded_off,
        ))
        contest = graph.raise_contest(
            node.id, ContestType.JUDGMENT, cp.raised_by,
            question_or_alternative=cp.alternative,
        )
        needs_user_decision_cards.append({
            "contest_id": contest.id,
            "current": node.chosen_value,
            "current_rationale": node.rationale,
            "alternative": cp.alternative,
            "alternative_rationale": cp.rationale,
        })

    high_severity_gaps = [g for g in merged["gaps"] if g.severity == "high"]

    return {
        "component": component,
        "needs_user_questions": needs_user_questions,
        "needs_user_decision_cards": needs_user_decision_cards,
        "high_severity_gaps": [g.__dict__ for g in high_severity_gaps],
        "low_priority_gaps": [g.__dict__ for g in merged["gaps"] if g.severity != "high"],
        "agents_with_errors": merged["agents_with_errors"],
    }
