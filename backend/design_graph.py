"""
Decision graph — the structured source of truth that replaces the
"whole DESIGN.md overwritten every turn" model.

DESIGN.md / the Mermaid diagram become *rendered views* of this graph.
Agents never write markdown directly; they propose/update/contest nodes,
and the orchestrator enforces write-scope and cascade invalidation here,
in code — not via prompt instructions agents might ignore.

Storage follows the same sqlite3 + threading.RLock + JSON-blob-column
convention as backend/storage.py, so this can live alongside it and
share the same `.agentflow/agentflow.db` file (call `attach(project_store._db)`
or run its own connection — see DesignGraphStore.__init__).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# ─── Enums ───────────────────────────────────────────────────────────────────

class NodeStatus(str, Enum):
    PROPOSED = "proposed"
    CONTESTED = "contested"
    RESOLVED = "resolved"
    STALE = "stale"          # a dependency changed; needs re-review
    SUPERSEDED = "superseded"  # replaced by a newer version of itself


class ContestType(str, Enum):
    MISSING_CONSTRAINT = "missing_constraint"  # resolvable by a fact, not a preference
    JUDGMENT = "judgment"                       # both sides have the same facts; needs a real tie-break


class ResolvedBy(str, Enum):
    USER = "user"
    AUTO = "auto"


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Dissent:
    agent: str
    alternative: str
    criteria_optimized: list[str] = field(default_factory=list)
    criteria_traded_off: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "alternative": self.alternative,
            "criteria_optimized": self.criteria_optimized,
            "criteria_traded_off": self.criteria_traded_off,
        }

    @staticmethod
    def from_dict(d: dict) -> "Dissent":
        return Dissent(
            agent=d["agent"],
            alternative=d["alternative"],
            criteria_optimized=d.get("criteria_optimized", []),
            criteria_traded_off=d.get("criteria_traded_off", []),
        )


@dataclass
class Contest:
    id: str
    type: ContestType
    raised_by: str
    question_or_alternative: str
    resolved: bool = False
    resolution: Optional[str] = None
    resolved_by: Optional[ResolvedBy] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "raised_by": self.raised_by,
            "question_or_alternative": self.question_or_alternative,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "resolved_by": self.resolved_by.value if self.resolved_by else None,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Contest":
        return Contest(
            id=d["id"],
            type=ContestType(d["type"]),
            raised_by=d["raised_by"],
            question_or_alternative=d["question_or_alternative"],
            resolved=d.get("resolved", False),
            resolution=d.get("resolution"),
            resolved_by=ResolvedBy(d["resolved_by"]) if d.get("resolved_by") else None,
            created_at=d.get("created_at", ""),
        )


@dataclass
class DecisionNode:
    id: str
    component: str                     # e.g. "logging", "database", "auth"
    status: NodeStatus
    chosen_value: str
    rationale: str
    proposed_by: str
    depends_on: list[str] = field(default_factory=list)   # constraint_id or decision_id
    affects: list[str] = field(default_factory=list)      # decision_ids to flag stale on change
    dissent: list[Dissent] = field(default_factory=list)
    contests: list[Contest] = field(default_factory=list)
    version: int = 1
    superseded_by: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ConstraintNode:
    id: str
    key: str          # e.g. "deployment_target"
    value: str        # e.g. "self-hosted"
    source: str       # "user_intake" | "inferred"
    locked: bool = False
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Store ───────────────────────────────────────────────────────────────────

class DesignGraphStore:
    """Per-project store for decision/constraint nodes.

    Mirrors ProjectStore's connection style so it can either share a
    project's existing sqlite3.Connection (pass `db=`) or open its own
    file (pass `metadata_dir=`).
    """

    def __init__(self, metadata_dir: Optional[Path] = None, db: Optional[sqlite3.Connection] = None):
        self._lock = threading.RLock()
        if db is not None:
            self._db = db
        else:
            if metadata_dir is None:
                raise ValueError("Provide either metadata_dir or an existing db connection")
            metadata_dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(metadata_dir / "agentflow.db", check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_nodes (
                    id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chosen_value TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    affects_json TEXT NOT NULL DEFAULT '[]',
                    dissent_json TEXT NOT NULL DEFAULT '[]',
                    contests_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    superseded_by TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_component ON decision_nodes(component);

                CREATE TABLE IF NOT EXISTS constraint_nodes (
                    id TEXT PRIMARY KEY,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    locked INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ── Row <-> dataclass ────────────────────────────────────────────────────

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> DecisionNode:
        return DecisionNode(
            id=row["id"],
            component=row["component"],
            status=NodeStatus(row["status"]),
            chosen_value=row["chosen_value"],
            rationale=row["rationale"],
            proposed_by=row["proposed_by"],
            depends_on=json.loads(row["depends_on_json"]),
            affects=json.loads(row["affects_json"]),
            dissent=[Dissent.from_dict(d) for d in json.loads(row["dissent_json"])],
            contests=[Contest.from_dict(c) for c in json.loads(row["contests_json"])],
            version=row["version"],
            superseded_by=row["superseded_by"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_constraint(row: sqlite3.Row) -> ConstraintNode:
        return ConstraintNode(
            id=row["id"], key=row["key"], value=row["value"],
            source=row["source"], locked=bool(row["locked"]), updated_at=row["updated_at"],
        )

    # ── Decision nodes ───────────────────────────────────────────────────────

    def propose_decision(
        self, component: str, chosen_value: str, rationale: str, proposed_by: str,
        depends_on: Optional[list[str]] = None, affects: Optional[list[str]] = None,
    ) -> DecisionNode:
        """Create a new decision node. Raises if an unresolved node already
        exists for this component — callers should update_decision instead."""
        existing = self.get_active_decision_for_component(component)
        if existing is not None:
            raise ValueError(
                f"Component '{component}' already has an active decision ({existing.id}); "
                f"use update_decision or resolve its contests first."
            )
        node = DecisionNode(
            id=str(uuid.uuid4()), component=component, status=NodeStatus.PROPOSED,
            chosen_value=chosen_value, rationale=rationale, proposed_by=proposed_by,
            depends_on=depends_on or [], affects=affects or [],
        )
        self._upsert_decision(node)
        return node

    def _upsert_decision(self, node: DecisionNode):
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO decision_nodes
                   (id, component, status, chosen_value, rationale, proposed_by,
                    depends_on_json, affects_json, dissent_json, contests_json,
                    version, superseded_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       component=excluded.component, status=excluded.status,
                       chosen_value=excluded.chosen_value, rationale=excluded.rationale,
                       depends_on_json=excluded.depends_on_json, affects_json=excluded.affects_json,
                       dissent_json=excluded.dissent_json, contests_json=excluded.contests_json,
                       version=excluded.version, superseded_by=excluded.superseded_by,
                       updated_at=excluded.updated_at""",
                (
                    node.id, node.component, node.status.value, node.chosen_value,
                    node.rationale, node.proposed_by, json.dumps(node.depends_on),
                    json.dumps(node.affects), json.dumps([d.to_dict() for d in node.dissent]),
                    json.dumps([c.to_dict() for c in node.contests]), node.version,
                    node.superseded_by, datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_decision(self, decision_id: str) -> Optional[DecisionNode]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM decision_nodes WHERE id = ?", (decision_id,)
            ).fetchone()
        return self._row_to_decision(row) if row else None

    def get_active_decision_for_component(self, component: str) -> Optional[DecisionNode]:
        """The current (non-superseded) node for a component, if any."""
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM decision_nodes
                   WHERE component = ? AND status != ?
                   ORDER BY version DESC LIMIT 1""",
                (component, NodeStatus.SUPERSEDED.value),
            ).fetchone()
        return self._row_to_decision(row) if row else None

    def all_decisions(self, include_superseded: bool = False) -> list[DecisionNode]:
        with self._lock:
            if include_superseded:
                rows = self._db.execute("SELECT * FROM decision_nodes ORDER BY component").fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM decision_nodes WHERE status != ? ORDER BY component",
                    (NodeStatus.SUPERSEDED.value,),
                ).fetchall()
        return [self._row_to_decision(r) for r in rows]

    def revise_decision(
        self, decision_id: str, new_value: str, rationale: str, proposed_by: str,
    ) -> DecisionNode:
        """Supersede a resolved decision with a new version (e.g. after a
        constraint change forced re-review). Preserves depends_on/affects
        unless the caller updates them afterward."""
        old = self.get_decision(decision_id)
        if old is None:
            raise ValueError(f"No such decision: {decision_id}")
        old.status = NodeStatus.SUPERSEDED
        new_node = DecisionNode(
            id=str(uuid.uuid4()), component=old.component, status=NodeStatus.PROPOSED,
            chosen_value=new_value, rationale=rationale, proposed_by=proposed_by,
            depends_on=list(old.depends_on), affects=list(old.affects),
            version=old.version + 1,
        )
        old.superseded_by = new_node.id
        with self._lock, self._db:
            self._upsert_decision(old)
            self._upsert_decision(new_node)
        return new_node

    # ── Contests & dissent ───────────────────────────────────────────────────

    def add_dissent(self, decision_id: str, dissent: Dissent):
        node = self.get_decision(decision_id)
        if node is None:
            raise ValueError(f"No such decision: {decision_id}")
        node.dissent.append(dissent)
        self._upsert_decision(node)

    def raise_contest(
        self, decision_id: str, contest_type: ContestType, raised_by: str,
        question_or_alternative: str,
    ) -> Contest:
        node = self.get_decision(decision_id)
        if node is None:
            raise ValueError(f"No such decision: {decision_id}")
        contest = Contest(
            id=str(uuid.uuid4()), type=contest_type, raised_by=raised_by,
            question_or_alternative=question_or_alternative,
        )
        node.contests.append(contest)
        node.status = NodeStatus.CONTESTED
        self._upsert_decision(node)
        return contest

    def resolve_contest(
        self, decision_id: str, contest_id: str, resolution: str, resolved_by: ResolvedBy,
    ):
        node = self.get_decision(decision_id)
        if node is None:
            raise ValueError(f"No such decision: {decision_id}")
        for c in node.contests:
            if c.id == contest_id:
                c.resolved = True
                c.resolution = resolution
                c.resolved_by = resolved_by
                break
        else:
            raise ValueError(f"No such contest: {contest_id}")
        if all(c.resolved for c in node.contests):
            node.status = NodeStatus.RESOLVED
        self._upsert_decision(node)

    def unresolved_contests(self) -> list[tuple[DecisionNode, Contest]]:
        out = []
        for node in self.all_decisions():
            for c in node.contests:
                if not c.resolved:
                    out.append((node, c))
        return out

    # ── Constraints ──────────────────────────────────────────────────────────

    def set_constraint(self, key: str, value: str, source: str, locked: bool = False) -> ConstraintNode:
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM constraint_nodes WHERE key = ?", (key,)
            ).fetchone()
        if existing and bool(existing["locked"]) and source != "user_intake":
            # locked constraints can't be silently reinterpreted by an agent
            raise ValueError(f"Constraint '{key}' is locked; only the user can change it")

        node = ConstraintNode(
            id=existing["id"] if existing else str(uuid.uuid4()),
            key=key, value=value, source=source, locked=locked,
        )
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO constraint_nodes (id, key, value, source, locked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, source=excluded.source,
                       locked=excluded.locked, updated_at=excluded.updated_at""",
                (node.id, node.key, node.value, node.source, int(node.locked),
                 datetime.now(timezone.utc).isoformat()),
            )
        changed_downstream = []
        if existing and existing["value"] != value:
            changed_downstream = self.cascade_stale(node.id)
        return node, changed_downstream

    def get_constraint(self, key: str) -> Optional[ConstraintNode]:
        with self._lock:
            row = self._db.execute("SELECT * FROM constraint_nodes WHERE key = ?", (key,)).fetchone()
        return self._row_to_constraint(row) if row else None

    def all_constraints(self) -> list[ConstraintNode]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM constraint_nodes ORDER BY key").fetchall()
        return [self._row_to_constraint(r) for r in rows]

    # ── Cascade invalidation ─────────────────────────────────────────────────

    def cascade_stale(self, changed_node_id: str) -> list[str]:
        """When a constraint or decision changes, walk `affects` edges and
        flip dependent decisions to STALE. Returns the ids flipped.
        Does NOT auto-recompute anything — staleness only triggers a
        re-review pass, never a silent value change."""
        flipped: list[str] = []
        to_visit = [changed_node_id]
        visited = set()
        all_nodes = {n.id: n for n in self.all_decisions()}

        while to_visit:
            current = to_visit.pop()
            if current in visited:
                continue
            visited.add(current)
            for node in all_nodes.values():
                if current in node.depends_on and node.status != NodeStatus.STALE:
                    node.status = NodeStatus.STALE
                    self._upsert_decision(node)
                    flipped.append(node.id)
                    to_visit.append(node.id)
        return flipped

    # ── Scoped context for a debate ──────────────────────────────────────────

    def dependency_closure_context(self, component: str) -> dict:
        """The read-only context a scoped debate on `component` should get:
        its own current node (if any), the resolved values of everything it
        depends_on (transitively), and the relevant constraints. This is what
        gets sent instead of the full design doc — mirrors Workspace's
        diff-based context but scoped by dependency graph instead of by file.
        """
        target = self.get_active_decision_for_component(component)
        all_nodes = {n.id: n for n in self.all_decisions()}
        constraints = {c.id: c for c in self.all_constraints()}

        resolved_deps: dict[str, str] = {}
        to_visit = list(target.depends_on) if target else []
        visited = set()
        while to_visit:
            dep_id = to_visit.pop()
            if dep_id in visited:
                continue
            visited.add(dep_id)
            if dep_id in all_nodes:
                dep = all_nodes[dep_id]
                resolved_deps[dep.component] = dep.chosen_value
                to_visit.extend(dep.depends_on)
            elif dep_id in constraints:
                c = constraints[dep_id]
                resolved_deps[c.key] = c.value

        return {
            "component": component,
            "current_decision": {
                "chosen_value": target.chosen_value,
                "rationale": target.rationale,
                "status": target.status.value,
            } if target else None,
            "resolved_dependencies": resolved_deps,
        }

    # ── Rendering (graph -> markdown/mermaid view) ──────────────────────────

    def render_design_md(self, idea: str) -> str:
        """Generates DESIGN.md content FROM the graph. Agents should never
        write this file directly — see module docstring."""
        lines = [f"# Design Document\n**Idea:** {idea}\n"]

        constraints = self.all_constraints()
        if constraints:
            lines.append("## Constraints\n")
            for c in constraints:
                lock = " (locked)" if c.locked else ""
                lines.append(f"- **{c.key}**: {c.value}{lock}")
            lines.append("")

        lines.append("## Decisions\n")
        for node in self.all_decisions():
            lines.append(f"### {node.component}")
            lines.append(f"**Choice:** {node.chosen_value}  ")
            lines.append(f"**Status:** {node.status.value}  ")
            lines.append(f"**Rationale:** {node.rationale}\n")
            if node.dissent:
                lines.append("**Dissenting views:**")
                for d in node.dissent:
                    lines.append(f"- *{d.agent}* proposed: {d.alternative}")
            unresolved = [c for c in node.contests if not c.resolved]
            if unresolved:
                lines.append("**Open contests:**")
                for c in unresolved:
                    lines.append(f"- [{c.type.value}] {c.question_or_alternative} (raised by {c.raised_by})")
            lines.append("")

        lines.append("## Architecture Diagram\n")
        lines.append("```mermaid")
        lines.append(self.render_mermaid())
        lines.append("```")
        return "\n".join(lines)

    def render_mermaid(self) -> str:
        decisions = self.all_decisions()
        out = ["graph TD"]
        for node in decisions:
            label = f"{node.component}: {node.chosen_value}".replace('"', "'")
            style = ""
            if node.status == NodeStatus.STALE:
                style = f'\n    style {node.component} fill:#f66,stroke:#900'
            elif node.status == NodeStatus.CONTESTED:
                style = f'\n    style {node.component} fill:#fc6,stroke:#960'
            out.append(f'    {node.component}["{label}"]{style}')
        id_to_component = {n.id: n.component for n in decisions}
        for node in decisions:
            for dep_id in node.depends_on:
                if dep_id in id_to_component:
                    out.append(f"    {id_to_component[dep_id]} --> {node.component}")
        return "\n".join(out)
