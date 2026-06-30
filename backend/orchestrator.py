"""
Orchestrator — runs debate + build phases.
All state changes emit events via an async queue so the UI gets live updates.
Human steering: pause, inject a message into the debate, swap agent roles.
"""

from __future__ import annotations
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

from .agents.base import AgentBase
from .workspace.workspace import Workspace


# ─── Events ──────────────────────────────────────────────────────────────────

class EventKind(str, Enum):
    PHASE       = "phase"        # phase change
    TURN_START  = "turn_start"   # agent about to speak
    TURN_END    = "turn_end"     # agent finished, includes response
    VOTE        = "vote"         # consensus vote cast
    VERDICT     = "verdict"      # reviewer/tester verdict
    CONSENSUS   = "consensus"    # all agreed
    FILE_WRITE  = "file_write"   # workspace file updated
    STEER       = "steer"        # human injected a message
    DONE        = "done"         # entire run finished
    ERROR       = "error"        # something failed
    RETRY       = "retry"        # agent is waiting for a usage limit reset


@dataclass
class Event:
    kind: EventKind
    agent: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "agent": self.agent,
            "data": self.data,
            "timestamp": self.timestamp,
        }


# ─── Prompts ─────────────────────────────────────────────────────────────────

DEBATE_SYSTEM = """You are one of {n} agents collaborating to design a software product.
Each agent may have a standing perspective. Contribute through your own expertise while
challenging weak ideas and building on strong ones. Be specific and opinionated.

Respond in this exact format:

## DESIGN_APPEND
<your contribution to the design — proposal, critique, or refinement>

## PLAN_UPDATE
<complete updated content of PLAN.md — keep existing tasks, add/revise yours>

## CONSENSUS_APPEND
<your reasoning this round>
VOTE: AGREE
or
VOTE: DISAGREE
<one sentence reason>

Only VOTE: AGREE when you genuinely believe the design is solid and complete."""

BUILD_SYSTEMS = {
    "developer": """You are the DEVELOPER this iteration.
Read the workspace and write or update source code based on the design and any review feedback.

Respond in this exact format:

## FILE: src/filename.py
<complete file content — no markdown fences>

## FILE: src/another_file.py
<complete file content>

## PLAN_UPDATE
<updated PLAN.md — check off completed tasks with [x]>""",

    "reviewer": """You are the CODE REVIEWER this iteration.
Read the code and design carefully. Add inline comments as # REVIEW: your note
directly in the source files. Update the design document with architectural notes.

Respond in this exact format:

## FILE: src/filename.py
<file content with your # REVIEW: comments added inline>

## DESIGN_APPEND
<architectural notes, concerns, or decisions>

## VERDICT
APPROVE
or
CHANGES NEEDED
<specific issues that must be fixed>""",

    "tester": """You are the TESTER this iteration.
Read the code and evaluate it against the plan. Write test cases, describe expected
vs actual behavior, and record results.

Respond in this exact format:

## TEST_RESULTS_APPEND
<your test run: list test cases, results, failures>

## PLAN_UPDATE
<updated PLAN.md — check off tested items, add bug tasks if needed>

## VERDICT
PASS
or
FAIL
<specific failures and what needs to change>""",
}

ROLE_NEEDS = {
    "developer": ["plan", "src"],
    "reviewer":  ["design", "src"],
    "tester":    ["plan", "src", "tests"],
}


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(
        self,
        agents: list[AgentBase],
        workspace: Workspace,
        event_cb: Optional[Callable[[Event], Any]] = None,
        max_debate_rounds: int = 6,
        max_build_iterations: int = 5,
    ):
        self.agents = agents
        self.ws = workspace
        self._cb = event_cb
        self.max_debate_rounds = max_debate_rounds
        self.max_build_iterations = max_build_iterations

        # Steering controls
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._steer_queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False

    # ── Public controls ───────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        self._pause_event.clear()

    def resume(self):
        self._paused = False
        self._pause_event.set()

    async def steer(self, message: str):
        """Inject a human message that all agents will see next turn."""
        await self._steer_queue.put(message)
        self._emit(Event(EventKind.STEER, agent="human", data={"message": message}))

    def stop(self):
        self._running = False

    # ── Main entry ────────────────────────────────────────────────────────────

    async def run(self, idea: str):
        self._running = True
        self.idea = idea
        self.ws.init(idea)
        n = len(self.agents)

        # The first useful turn carries the seed. This avoids one full model
        # generation per agent whose only purpose used to be "wait".
        await self._debate_phase(n)
        if not self._running:
            return

        await self._build_phase()
        return self.ws.snapshot()

    # ── Debate phase ──────────────────────────────────────────────────────────

    async def _debate_phase(self, n: int):
        self._emit(Event(EventKind.PHASE, data={"phase": "debate"}))

        for round_num in range(1, self.max_debate_rounds + 1):
            await self._wait_if_paused()
            if not self._running:
                return

            votes: dict[str, str] = {}
            steering = await self._drain_steer()

            for agent in self.agents:
                await self._wait_if_paused()
                delta = self.ws.changed_context(agent.name)
                steer_block = f"\n\n[HUMAN STEERING]\n{steering}" if steering else ""
                seed_block = f"\n\nProduct idea: {self.idea}" if round_num == 1 else ""
                prompt = (
                    f"{DEBATE_SYSTEM.format(n=len(self.agents))}"
                    f"{seed_block}\n\n"
                    f"Debate round {round_num}/{self.max_debate_rounds}.\n\n"
                    f"Changes since your last turn:\n{delta}{steer_block}\n\n"
                    "Add your contributions now."
                )

                self._emit(Event(EventKind.TURN_START, agent=agent.name,
                                 data={"round": round_num, "phase": "debate",
                                       "standing_role": agent.config.role}))

                response = await self._send_agent(agent, prompt)

                self._apply_debate_response(agent.name, round_num, response)
                vote = self.ws.parse_vote(response)
                votes[agent.name] = vote

                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "round": round_num, "response": response,
                    **self._usage_event(agent),
                }))
                self._emit(Event(EventKind.VOTE, agent=agent.name,
                                 data={"vote": vote, "round": round_num}))

            if all(v == "AGREE" for v in votes.values()):
                self._emit(Event(EventKind.CONSENSUS, data={
                    "round": round_num, "votes": votes
                }))
                return

            disagree = [a for a, v in votes.items() if v == "DISAGREE"]
            self._emit(Event(EventKind.PHASE, data={
                "phase": "debate", "round": round_num,
                "status": f"no consensus — {disagree} disagree"
            }))

        # Max rounds: proceed anyway
        self._emit(Event(EventKind.CONSENSUS, data={"forced": True, "votes": {}}))

    def _apply_debate_response(self, agent: str, round_num: int, response: str):
        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        consensus_bit = self.ws.parse_section(response, "CONSENSUS_APPEND")

        if design_bit:
            self.ws.append("design", design_bit, agent, f"Round {round_num}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent,
                             data={"file": "DESIGN.md", "preview": design_bit[:120]}))
        if plan_update:
            self.ws.write("plan", f"# Plan\n\n{plan_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent,
                             data={"file": "PLAN.md", "preview": plan_update[:120]}))
        if consensus_bit:
            self.ws.append("consensus", consensus_bit, agent, f"Round {round_num}")

    # ── Build phase ───────────────────────────────────────────────────────────

    async def _build_phase(self):
        self._emit(Event(EventKind.PHASE, data={"phase": "build"}))
        n = len(self.agents)

        for iteration in range(1, self.max_build_iterations + 1):
            await self._wait_if_paused()
            if not self._running:
                return

            # Rotate roles
            shifted = self.agents[iteration % n:] + self.agents[:iteration % n]
            roles = {
                "developer": shifted[0],
                "reviewer":  shifted[1 % n],
                "tester":    shifted[2 % n],
            }
            self._emit(Event(EventKind.PHASE, data={
                "phase": "build", "iteration": iteration,
                "roles": {r: a.name for r, a in roles.items()},
            }))

            verdicts: dict[str, str] = {}
            steering = await self._drain_steer()

            for role, agent in roles.items():
                await self._wait_if_paused()
                delta = self.ws.changed_context(agent.name, ROLE_NEEDS[role])
                steer_block = f"\n\n[HUMAN STEERING]\n{steering}" if steering else ""
                prompt = (
                    "Debate is complete; continue in the BUILD phase.\n"
                    f"Build iteration {iteration}. Your role: {role.upper()}.\n\n"
                    f"Workspace changes relevant to you:\n{delta}{steer_block}\n\n"
                    f"{BUILD_SYSTEMS[role]}"
                )

                self._emit(Event(EventKind.TURN_START, agent=agent.name,
                                 data={"iteration": iteration, "role": role,
                                       "standing_role": agent.config.role}))

                response = await self._send_agent(agent, prompt)

                verdict = self._apply_build_response(agent.name, role, iteration, response)
                verdicts[role] = verdict

                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "iteration": iteration, "role": role,
                    "response": response,
                    **self._usage_event(agent),
                }))
                self._emit(Event(EventKind.VERDICT, agent=agent.name,
                                 data={"role": role, "verdict": verdict, "iteration": iteration}))

            if verdicts.get("reviewer") == "APPROVE" and verdicts.get("tester") == "PASS":
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "build", "status": "complete", "iteration": iteration
                }))
                return

        self._emit(Event(EventKind.PHASE, data={"phase": "build", "status": "max_iterations"}))

    def _apply_build_response(self, agent: str, role: str, iteration: int, response: str) -> str:
        for filename, content in self.ws.parse_files(response).items():
            self.ws.write_src(filename, content)
            self._emit(Event(EventKind.FILE_WRITE, agent=agent,
                             data={"file": filename, "preview": content[:120]}))

        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        if plan_update:
            self.ws.write("plan", f"# Plan\n\n{plan_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent, data={"file": "PLAN.md"}))

        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        if design_bit:
            self.ws.append("design", design_bit, agent, f"Review iter {iteration}")

        test_bit = self.ws.parse_section(response, "TEST_RESULTS_APPEND")
        if test_bit:
            self.ws.append("tests", test_bit, agent, f"Iter {iteration}")

        return self.ws.parse_verdict(response, role)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _emit(self, event: Event):
        if self._cb:
            try:
                self._cb(event)
            except Exception:
                pass

    @staticmethod
    def _usage_event(agent: AgentBase) -> dict:
        usage = agent.last_usage.to_dict()
        return {
            "tokens": usage["total_tokens"],
            "usage": usage,
            "agent_totals": agent.usage_dict(),
        }

    @staticmethod
    def _agent_system(agent: AgentBase) -> str:
        identity = f"You are {agent.name}."
        if agent.config.role:
            identity += f" Your standing role and perspective is: {agent.config.role}."
        if agent.config.system_prompt:
            identity += f"\n\nBehavior instructions:\n{agent.config.system_prompt}"
        return identity

    async def _send_agent(self, agent: AgentBase, prompt: str) -> str:
        attempt = 0
        max_retries = int(agent.config.extra.get("rate_limit_max_retries", 0) or 0)
        while self._running:
            try:
                return await asyncio.to_thread(
                    agent.send, prompt, self._agent_system(agent)
                )
            except RuntimeError as exc:
                if not self._is_rate_limit(exc):
                    raise
                attempt += 1
                if max_retries and attempt > max_retries:
                    raise
                delay = self._retry_delay(exc, attempt, agent)
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                agent.mark_waiting(retry_at.isoformat(), str(exc))
                self._emit(Event(EventKind.RETRY, agent=agent.name, data={
                    "attempt": attempt,
                    "retry_in_seconds": delay,
                    "retry_at": retry_at.isoformat(),
                    "reason": str(exc),
                }))
                remaining = delay
                while remaining > 0 and self._running:
                    step = min(5, remaining)
                    await asyncio.sleep(step)
                    remaining -= step
        raise asyncio.CancelledError()

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = (
            "rate limit", "usage limit", "quota exceeded", "quota exhausted",
            "resource exhausted", "too many requests", "status code: 429",
            "error 429", "limit reached",
        )
        return any(marker in message for marker in markers)

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int, agent: AgentBase) -> int:
        message = str(exc).lower()
        match = re.search(
            r"(?:retry|try again|resets?)\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?",
            message,
        )
        if match:
            value = float(match.group(1))
            unit = match.group(2) or "seconds"
            if unit.startswith(("h", "hr")):
                value *= 3600
            elif unit.startswith(("m", "min")):
                value *= 60
            return max(1, int(value))
        clock = re.search(r"(?:retry|try again|resets?)\s+(?:at|on)\s+(\d{1,2}:\d{2}\s*(?:am|pm))", message)
        if clock:
            now = datetime.now().astimezone()
            target_time = datetime.strptime(clock.group(1).upper(), "%I:%M %p").time()
            target = datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)
            if target <= now:
                target += timedelta(days=1)
            return max(1, int((target - now).total_seconds()))
        base = int(agent.config.extra.get("rate_limit_retry_seconds", 30) or 30)
        cap = int(agent.config.extra.get("rate_limit_max_wait_seconds", 900) or 900)
        return min(cap, base * (2 ** min(attempt - 1, 6)))

    async def _wait_if_paused(self):
        await self._pause_event.wait()

    async def _drain_steer(self) -> str:
        msgs = []
        while not self._steer_queue.empty():
            msgs.append(await self._steer_queue.get())
        return "\n".join(msgs)
