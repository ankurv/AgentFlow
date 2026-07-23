"""
Closes the loop that apply_merge_to_graph() opens: takes what the user
decided about a contest and turns it into graph mutations, distinguishing
the two contest types the same way everything upstream does — a
missing_constraint answer is a fact that may cascade to other components; a
judgment answer only affects the one contested decision (its dissent is kept
either way, since "who argued what" is worth preserving even when they lose).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from design_graph import DesignGraphStore, ResolvedBy


@dataclass
class ResolutionResult:
    decision_id: str
    contest_id: str
    new_decision_id: Optional[str]     # set only if a judgment answer switched values
    stale_components: list[str]        # components that now need re-review (missing_constraint path only)


def resolve_missing_constraint(
    graph: DesignGraphStore,
    decision_id: str,
    contest_id: str,
    constraint_key: str,
    constraint_value: str,
) -> ResolutionResult:
    """User answered a factual question (e.g. 'is this self-hosted or
    cloud?'). Setting the constraint may cascade — anything downstream that
    depended on this being unset gets flagged STALE, not silently recomputed.
    The contest that prompted the question is resolved with the constraint
    value as its resolution text."""
    _, changed_downstream = graph.set_constraint(
        key=constraint_key, value=constraint_value, source="user_intake", locked=True,
    )
    graph.resolve_contest(
        decision_id, contest_id,
        resolution=f"Constraint '{constraint_key}' set to '{constraint_value}'",
        resolved_by=ResolvedBy.USER,
    )
    # The decision that raised the question is itself worth a re-look now
    # that the fact exists, even if it wasn't in its own affects list.
    stale = list(dict.fromkeys(changed_downstream))
    return ResolutionResult(
        decision_id=decision_id, contest_id=contest_id,
        new_decision_id=None, stale_components=stale,
    )


def resolve_judgment(
    graph: DesignGraphStore,
    decision_id: str,
    contest_id: str,
    choice: str,                    # "keep_current" | "switch"
    new_value: Optional[str] = None,
    new_rationale: Optional[str] = None,
) -> ResolutionResult:
    """User picked a side on a genuine trade-off. Unlike missing_constraint,
    this never cascades on its own — a judgment call about logging doesn't
    retroactively invalidate the database decision. If the design later
    needs to account for the switch elsewhere, that's a new gap a future
    review pass should catch, not something we infer here."""
    if choice not in ("keep_current", "switch"):
        raise ValueError("choice must be 'keep_current' or 'switch'")

    new_decision_id = None
    if choice == "switch":
        if not new_value:
            raise ValueError("new_value is required when choice == 'switch'")
        new_node = graph.revise_decision(
            decision_id, new_value=new_value,
            rationale=new_rationale or "User tie-break: switched based on reviewer proposal",
            proposed_by="user_tiebreak",
        )
        new_decision_id = new_node.id

    graph.resolve_contest(
        decision_id, contest_id,
        resolution=f"User chose: {choice}" + (f" -> {new_value}" if new_value else ""),
        resolved_by=ResolvedBy.USER,
    )
    return ResolutionResult(
        decision_id=decision_id, contest_id=contest_id,
        new_decision_id=new_decision_id, stale_components=[],
    )
