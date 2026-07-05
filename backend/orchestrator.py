"""
Orchestrator — runs debate + build phases.
All state changes emit events via an async queue so the UI gets live updates.
Human steering: pause, inject a message into the debate, swap agent roles.
"""

from __future__ import annotations
import asyncio
import os
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

COORDINATOR_SYSTEM = """You are the COORDINATOR of an autonomous software engineering team.
Your goal is to coordinate the team's agents to design, build, review, and test a software product based on the user's idea.

Current execution mode: {mode}

Guidelines for Design & Architectural Gathering:
1. **Collaborative Requirement Brainstorming**: If the user's initial prompt or idea is brief, ambiguous, or lacks performance constraints, do not pause immediately. Instead, first call other agents (such as the Architect or Reviewer) to brainstorm the architectural requirements, critique the brief, and list what open information is needed. Once the team has debated the requirements (e.g., for 1-2 turns), compile their open questions and present them to the user by setting ## NEXT_AGENT to USER, listing the compiled clarifying questions under ## INSTRUCTIONS, and setting the verdict to PAUSE_FOR_INPUT. Do not lock in a design blindly.
2. **Scalability Analysis**: When designing architecture in DESIGN.md, you must dedicate a section named "## Scalability, Bottlenecks & Design Choices". Analyze performance implications, caching, database indexing, and potential bottlenecks (e.g. locks, network hops, memory footprint).
3. **Architecture Diagrams**: ALWAYS include a visual flowchart of component connections under a "## Architecture Diagram" section in DESIGN.md using a code block tagged with "mermaid" (flowchart TD or LR). E.g.
   ```mermaid
   flowchart TD
     A[Frontend] --> B[API Server]
     B --> C[(Database)]
   ```

Depending on the mode, follow these structured instructions:
- **all**: Run Phase 1 (Planning & Design reviews), wait/pause for user review, and then proceed to Phase 2 (Task-by-task execution).
- **debate**: Focus ONLY on Phase 1 (High-Level Planning & Design reviews). Once the plans/designs are refined and reviewed, state VERDICT as COMPLETE. Do not proceed to Phase 2.
- **build**: Skip Phase 1 and jump directly to Phase 2 (Task-by-task execution) based on the tasks already listed in PLAN.md.

Structured workflow description:
1. **Phase 1 (High-Level Planning & Design)**:
   - Perform requirement gathering and design a high-level task list (ideally with subtasks) in PLAN.md. 
   - Define the initial design concept in DESIGN.md (with the Mermaid flowchart and scalability sections).
   - Call other agents (by setting ## NEXT_AGENT) to review, critique, and improve this high-level task list and design.
   - Once other agents have reviewed, present the plan/design to the human user for review by pausing or outputting the next step.
2. **Phase 2 (Task-by-task Debate & Execution)**:
   - Select the first pending task/subtask from PLAN.md.
   - Further debate this specific task/subtask. Prompt agents to create detailed design and specifications in ImplementationPlan.md.
   - Execute the task: call the developer to write code, call the reviewer to check, and call the tester to verify.
   - Once the task is completed and verified, check it off in PLAN.md with [x].
   - Repeat for each task/subtask in PLAN.md until all tasks are checked off.

Available agents in the team:
{agents_list}

Read the current workspace files carefully and decide what should happen next.
To run an agent, output their name under ## NEXT_AGENT and specify what they should do under ## INSTRUCTIONS.
If you need clarification from the human user to make the right design choices, output USER under ## NEXT_AGENT, state the question(s) under ## INSTRUCTIONS, and set the verdict to PAUSE_FOR_INPUT.
If you believe the design and implementation are fully complete, correct, and verified, output COMPLETE under ## VERDICT.

Respond in this exact format:

## NEXT_AGENT
<exact name of the agent to run next, or USER>

## INSTRUCTIONS
<specific instructions or guidance for this agent's turn, or your clarifying question for the user>

## VERDICT
<CONTINUE, COMPLETE, or PAUSE_FOR_INPUT>"""


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(
        self,
        agents: list[AgentBase],
        workspace: Workspace,
        event_cb: Optional[Callable[[Event], Any]] = None,
        max_debate_rounds: int = 6,
        max_build_iterations: int = 5,
        require_approval: bool = True,
        mode: str = "all",
    ):
        self.agents = agents
        self.ws = workspace
        self._cb = event_cb
        self.max_debate_rounds = max_debate_rounds
        self.max_build_iterations = max_build_iterations
        self.require_approval = require_approval
        self.mode = mode

        # Steering controls
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._steer_queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False
        self._turn_sequence = 0
        self._turn_attempts: dict[str, int] = {}
        self._failed_turn: dict[str, Any] | None = None
        self._recovery_event = asyncio.Event()

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
        self._pause_event.set()
        self._recovery_event.set()

    @property
    def failed_turn(self) -> dict[str, Any] | None:
        if not self._failed_turn:
            return None
        return {key: value for key, value in self._failed_turn.items() if key != "prompt"}

    def retry_failed_turn(self):
        if not self._failed_turn:
            raise ValueError("There is no failed turn to retry")
        self._paused = False
        self._pause_event.set()
        self._recovery_event.set()

    # ── Main entry ────────────────────────────────────────────────────────────

    async def run(self, idea: str):
        self._running = True
        self.idea = idea
        
        # Only initialize workspace files if design doesn't exist yet, OR if a fresh idea is provided and mode is not build
        design_file = self.ws._file("design")
        if not design_file.exists() or (idea.strip() and self.mode != "build"):
            self.ws.init(idea)
            
        n = len(self.agents)
        if n == 0:
            return self.ws.snapshot()

        coordinator = next((a for a in self.agents if a.config.extra.get("is_coordinator")), None)
        if not coordinator and not os.environ.get("AGENTFLOW_TEST"):
            # Pick the best model as the coordinator
            def get_model_score(agent: AgentBase) -> int:
                kind = (agent.config.kind or "").lower()
                model = (agent.config.model or "").lower()
                if kind == "claude":
                    if "opus" in model:
                        return 100
                    if "sonnet" in model:
                        return 95
                    return 85
                elif kind == "openai":
                    if "o1" in model or "o3" in model:
                        return 98
                    if "gpt-4" in model or "o1-mini" in model:
                        return 90
                    return 80
                elif kind == "gemini":
                    if "pro" in model:
                        return 88
                    if "flash" in model:
                        return 78
                    return 70
                elif kind == "ollama":
                    return 50
                elif kind == "cli":
                    return 10
                return 0

            coordinator = max(self.agents, key=get_model_score)

        # Check if the prompt explicitly requests a debate or discussion
        idea_lower = idea.lower()
        is_debate = any(k in idea_lower for k in ["debate", "discuss", "discussion", "project", "loop", "run debate"]) or os.environ.get("AGENTFLOW_TEST") == "1"

        if not is_debate:
            # Direct chat mode!
            target_agent = None
            prompt_text = idea
            import re
            match = re.search(r'@(\w+)', idea)
            if match:
                mention = match.group(1).lower()
                for agent in self.agents:
                    if agent.name.lower() == mention:
                        target_agent = agent
                        prompt_text = re.sub(rf'@{match.group(1)}\s*', '', idea, flags=re.IGNORECASE).strip()
                        break
            
            if not target_agent:
                target_agent = coordinator or self.agents[0]
            
            self._emit(Event(EventKind.PHASE, data={
                "phase": "direct_chat", "status": f"Direct chat with {target_agent.name}"
            }))
            
            turn_context = {"step": 1, "phase": "direct_chat", "standing_role": target_agent.config.role}
            turn_id = self._begin_turn(target_agent, turn_context)
            
            snapshot = self.ws.snapshot()
            prompt = (
                f"Conversational Turn.\n"
                f"User Prompt: {prompt_text}\n"
                f"Workspace context (if any):\n{snapshot}\n"
            )
            
            response = await self._send_agent(target_agent, prompt, turn_id, turn_context)
            
            self._emit(Event(EventKind.TURN_END, agent=target_agent.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
                "step": 1, "response": response,
                **self._usage_event(target_agent),
            }))
            
            self._running = False
            return self.ws.snapshot()

        if coordinator:
            await self._coordinator_loop(coordinator)
            return self.ws.snapshot()

        # If mode is "all" or "debate", run debate phase
        if self.mode in {"all", "debate"}:
            while self._running:
                # The first useful turn carries the seed. This avoids one full model
                # generation per agent whose only purpose used to be "wait".
                await self._debate_phase(n)
                if not self._running:
                    return

                if self.require_approval:
                    self.pause()
                    self._emit(Event(EventKind.PHASE, data={"phase": "debate", "status": "waiting_for_approval"}))
                    await self._wait_if_paused()
                    if not self._running:
                        return

                    if not self._steer_queue.empty():
                        self._emit(Event(EventKind.PHASE, data={"phase": "debate", "status": "continuing_debate"}))
                        continue

                break

        # If mode is "all" or "build", run build phase
        if self._running and self.mode in {"all", "build"}:
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
            steering_accumulated = ""

            for agent in self.agents:
                await self._wait_if_paused()
                if not self._running:
                    return
                new_steering = await self._drain_steer()
                if new_steering:
                    if steering_accumulated:
                        steering_accumulated += "\n" + new_steering
                    else:
                        steering_accumulated = new_steering

                full_ctx = self.ws.full_context()
                steer_block = f"\n\n[HUMAN STEERING]\n{steering_accumulated}" if steering_accumulated else ""
                seed_block = f"\n\nProduct idea: {self.idea}" if round_num == 1 else ""
                prompt = (
                    f"{DEBATE_SYSTEM.format(n=len(self.agents))}"
                    f"{seed_block}\n\n"
                    f"Debate round {round_num}/{self.max_debate_rounds}.\n\n"
                    f"Current Workspace snapshot:\n{full_ctx}{steer_block}\n\n"
                    "Add your contributions now."
                )

                turn_context = {"round": round_num, "phase": "debate",
                                "standing_role": agent.config.role}
                turn_id = self._begin_turn(agent, turn_context)

                response = await self._send_agent(agent, prompt, turn_id, turn_context)

                self._apply_debate_response(agent.name, round_num, response)
                vote = self.ws.parse_vote(response)
                votes[agent.name] = vote

                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
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

            if self.require_approval and round_num % 2 == 0 and round_num < self.max_debate_rounds:
                self.pause()
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "debate", "round": round_num,
                    "status": "waiting_for_continuation"
                }))
                await self._wait_if_paused()
                if not self._running:
                    return

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
            steering_accumulated = ""

            for role, agent in roles.items():
                await self._wait_if_paused()
                if not self._running:
                    return
                new_steering = await self._drain_steer()
                if new_steering:
                    if steering_accumulated:
                        steering_accumulated += "\n" + new_steering
                    else:
                        steering_accumulated = new_steering

                full_ctx = self.ws.full_context()
                steer_block = f"\n\n[HUMAN STEERING]\n{steering_accumulated}" if steering_accumulated else ""
                prompt = (
                    "Debate is complete; continue in the BUILD phase.\n"
                    f"Build iteration {iteration}. Your role: {role.upper()}.\n\n"
                    f"Current Workspace snapshot:\n{full_ctx}{steer_block}\n\n"
                    f"{BUILD_SYSTEMS[role]}"
                )

                turn_context = {"iteration": iteration, "role": role, "phase": "build",
                                "standing_role": agent.config.role}
                turn_id = self._begin_turn(agent, turn_context)

                response = await self._send_agent(agent, prompt, turn_id, turn_context)

                verdict = self._apply_build_response(agent.name, role, iteration, response)
                verdicts[role] = verdict

                self._emit(Event(EventKind.TURN_END, agent=agent.name, data={
                    "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
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

            if self.require_approval and iteration < self.max_build_iterations:
                self.pause()
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "build", "iteration": iteration,
                    "status": "waiting_for_continuation"
                }))
                await self._wait_if_paused()
                if not self._running:
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

    def _begin_turn(self, agent: AgentBase, context: dict) -> str:
        self._turn_sequence += 1
        turn_id = f"turn-{self._turn_sequence:04d}"
        self._turn_attempts[turn_id] = 1
        self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
            "turn_id": turn_id, "attempt": 1, **context,
        }))
        return turn_id

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

    async def _send_agent(
        self,
        agent: AgentBase,
        prompt: str,
        turn_id: str = "turn-manual",
        turn_context: Optional[dict] = None,
    ) -> str:
        attempt = self._turn_attempts.get(turn_id, 1)
        self._turn_attempts[turn_id] = attempt
        context = dict(turn_context or {})
        max_retries = int(agent.config.extra.get("rate_limit_max_retries", 0) or 0)
        while self._running:
            try:
                response = await asyncio.to_thread(
                    agent.send, prompt, self._agent_system(agent)
                )
                self._failed_turn = None
                return response
            except RuntimeError as exc:
                if not self._is_rate_limit(exc):
                    agent.mark_error(str(exc))
                    self._failed_turn = {
                        "turn_id": turn_id,
                        "attempt": attempt,
                        "agent_id": agent.config.id,
                        "agent": agent.name,
                        "error": str(exc),
                        "prompt": prompt,
                        **context,
                    }
                    self._recovery_event.clear()
                    self._paused = True
                    self._pause_event.clear()
                    self._emit(Event(EventKind.ERROR, agent=agent.name, data={
                        **self.failed_turn,
                        "recoverable": True,
                        "message": "Fix this agent's configuration, save it, then retry the failed turn.",
                    }))
                    await self._recovery_event.wait()
                    if not self._running:
                        raise asyncio.CancelledError()
                    attempt += 1
                    self._turn_attempts[turn_id] = attempt
                    agent.error_message = ""
                    self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                        "turn_id": turn_id, "attempt": attempt, "resumed": True,
                        "retry_reason": "manual_recovery", **context,
                    }))
                    continue
                if max_retries and attempt >= max_retries + 1:
                    raise
                delay = self._retry_delay(exc, attempt, agent)
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                agent.mark_waiting(retry_at.isoformat(), str(exc))
                self._emit(Event(EventKind.RETRY, agent=agent.name, data={
                    "turn_id": turn_id, "attempt": attempt,
                    "retry_in_seconds": delay,
                    "retry_at": retry_at.isoformat(),
                    "reason": str(exc),
                }))
                remaining = delay
                while remaining > 0 and self._running:
                    step = min(5, remaining)
                    await asyncio.sleep(step)
                    remaining -= step
                if self._running:
                    attempt += 1
                    self._turn_attempts[turn_id] = attempt
                    self._emit(Event(EventKind.TURN_START, agent=agent.name, data={
                        "turn_id": turn_id, "attempt": attempt, "resumed": True,
                        "retry_reason": "usage_limit", **context,
                    }))
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

    async def _coordinator_loop(self, coordinator: AgentBase):
        other_agents = [a for a in self.agents if a.name != coordinator.name]
        agents_list = "\n".join(f"- {a.name}: {a.config.role or 'Contributor'}" for a in other_agents)
        system_prompt = COORDINATOR_SYSTEM.format(agents_list=agents_list, mode=self.mode)

        orig_system = coordinator.config.system_prompt
        coordinator.config.system_prompt = system_prompt

        max_steps = 30
        for step in range(1, max_steps + 1):
            await self._wait_if_paused()
            if not self._running:
                break

            # 1. Call Coordinator
            delta = self.ws.snapshot()
            new_steering = await self._drain_steer()
            steer_block = f"\n\n[HUMAN STEERING]\n{new_steering}" if new_steering else ""
            seed_block = f"\n\nProduct idea: {self.idea}" if step == 1 else ""

            prompt = (
                f"Coordinator execution step {step}/{max_steps}.\n"
                f"Current Workspace snapshot:\n{delta}{steer_block}{seed_block}\n\n"
                "Determine the NEXT_AGENT to run, provide INSTRUCTIONS, and state the VERDICT (CONTINUE or COMPLETE)."
            )

            turn_context = {"step": step, "phase": "coordinator", "standing_role": coordinator.config.role}
            turn_id = self._begin_turn(coordinator, turn_context)

            response = await self._send_agent(coordinator, prompt, turn_id, turn_context)

            # Apply coordinator's own plan/design updates or file writes
            self._apply_coordinator_agent_response(coordinator.name, response)

            self._emit(Event(EventKind.TURN_END, agent=coordinator.name, data={
                "turn_id": turn_id, "attempt": self._turn_attempts[turn_id],
                "step": step, "response": response,
                **self._usage_event(coordinator),
            }))

            next_agent_name, instructions, verdict = self._parse_coordinator_response(response)

            if verdict in {"COMPLETE", "PAUSE", "PAUSE_FOR_INPUT"}:
                status = "complete" if verdict == "COMPLETE" else "waiting_for_approval"
                self._emit(Event(EventKind.PHASE, data={
                    "phase": "coordinator", "status": status, "step": step
                }))
                if verdict != "COMPLETE":
                    self.pause()
                    await self._wait_if_paused()
                    if not self._running:
                        break
                    continue
                break

            # Find selected agent
            selected_agent = next((a for a in other_agents if a.name.lower() == next_agent_name.lower()), None)
            if not selected_agent:
                error_msg = f"Coordinator selected invalid agent: '{next_agent_name}'"
                self._emit(Event(EventKind.ERROR, agent=coordinator.name, data={"error": error_msg}))
                if other_agents:
                    selected_agent = other_agents[0]
                else:
                    break

            # 2. Call selected agent
            agent_full_ctx = self.ws.full_context()
            agent_prompt = (
                f"Coordinator Instructions:\n{instructions}\n\n"
                f"Current Workspace snapshot:\n{agent_full_ctx}\n\n"
                f"Please execute your turn now."
            )

            agent_turn_context = {"step": step, "phase": "coordinator_agent", "standing_role": selected_agent.config.role}
            agent_turn_id = self._begin_turn(selected_agent, agent_turn_context)

            agent_response = await self._send_agent(selected_agent, agent_prompt, agent_turn_id, agent_turn_context)

            self._apply_coordinator_agent_response(selected_agent.name, agent_response)

            self._emit(Event(EventKind.TURN_END, agent=selected_agent.name, data={
                "turn_id": agent_turn_id, "attempt": self._turn_attempts[agent_turn_id],
                "step": step, "response": agent_response,
                **self._usage_event(selected_agent),
            }))



        coordinator.config.system_prompt = orig_system

    def _parse_coordinator_response(self, response: str) -> tuple[str, str, str]:
        next_agent = self.ws.parse_section(response, "NEXT_AGENT").strip()
        instructions = self.ws.parse_section(response, "INSTRUCTIONS").strip()
        verdict = self.ws.parse_section(response, "VERDICT").strip().upper()
        return next_agent, instructions, verdict

    def _apply_coordinator_agent_response(self, agent_name: str, response: str):
        for filename, content in self.ws.parse_files(response).items():
            self.ws.write_src(filename, content)
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name,
                             data={"file": filename, "preview": content[:120]}))

        plan_update = self.ws.parse_section(response, "PLAN_UPDATE")
        if plan_update:
            self.ws.write("plan", f"# Plan\n\n{plan_update}")
            self._emit(Event(EventKind.FILE_WRITE, agent=agent_name, data={"file": "PLAN.md"}))

        design_bit = self.ws.parse_section(response, "DESIGN_APPEND")
        if design_bit:
            self.ws.append("design", design_bit, agent_name, "Coordinator-led Turn")

        test_bit = self.ws.parse_section(response, "TEST_RESULTS_APPEND")
        if test_bit:
            self.ws.append("tests", test_bit, agent_name, "Coordinator-led Turn")
