import asyncio
import json
import subprocess
import tempfile
import unittest

from backend.agents.base import AgentBase, AgentConfig, Usage
from backend.agents.providers import CLIAgent
from backend.orchestrator import Orchestrator
from backend.storage import ProjectStore
from backend.workspace.workspace import Workspace


class StatefulFake(AgentBase):
    manages_context = True

    def __init__(self, config, replies=None):
        super().__init__(config)
        self.received = []
        self.received_systems = []
        self.replies = iter(replies or ["ok"])

    def _raw_send(self, messages, system):
        self.received.append(messages)
        self.received_systems.append(system)
        return next(self.replies), Usage(
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=20,
        )


class FakeCLI(CLIAgent):
    def __init__(self, config, outputs):
        self.outputs = iter(outputs)
        self.commands = []
        super().__init__(config)

    def _run(self, argv, cwd=None):
        self.commands.append((argv, cwd))
        stdout = next(self.outputs)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class RateLimitedFake(StatefulFake):
    def __init__(self, config, reply):
        super().__init__(config, replies=[reply])
        self.attempts = 0

    def _raw_send(self, messages, system):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("429 usage limit reached; retry after 1 second")
        return super()._raw_send(messages, system)


class ImmediateRetryOrchestrator(Orchestrator):
    @staticmethod
    def _retry_delay(exc, attempt, agent):
        return 0


class SessionTests(unittest.TestCase):
    def test_stateful_agent_sends_only_new_turn_and_tracks_cost(self):
        agent = StatefulFake(
            AgentConfig(
                name="stateful",
                kind="openai",
                model="gpt-4o",
            ),
            replies=["one", "two"],
        )
        agent.send("first")
        agent.send("second")

        self.assertEqual(agent.received[0], [{"role": "user", "content": "first"}])
        self.assertEqual(agent.received[1], [{"role": "user", "content": "second"}])
        self.assertEqual(agent.total_tokens, 240)
        self.assertEqual(agent.total_cached_input_tokens, 80)
        self.assertGreater(agent.total_cost_usd, 0)

    def test_codex_cli_resumes_exact_thread(self):
        first = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "abc-123"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first reply"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4,
            }}),
        ])
        second = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second reply"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 12, "cached_input_tokens": 8, "output_tokens": 3,
            }}),
        ])
        agent = FakeCLI(
            AgentConfig(
                name="codex",
                kind="cli",
                cli_command="codex exec --ephemeral --skip-git-repo-check",
            ),
            [first, second],
        )

        self.assertEqual(agent.send("turn one"), "first reply")
        self.assertEqual(agent.send("turn two"), "second reply")

        first_command = agent.commands[0][0]
        second_command = agent.commands[1][0]
        self.assertNotIn("--ephemeral", first_command)
        self.assertIn("--json", first_command)
        self.assertEqual(second_command[:3], ["codex", "exec", "resume"])
        self.assertIn("abc-123", second_command)
        self.assertEqual(second_command[-1], "turn two")
        self.assertNotIn("turn one", second_command[-1])
        self.assertEqual(agent.total_cached_input_tokens, 10)

    def test_antigravity_uses_isolated_continue_session(self):
        agent = FakeCLI(
            AgentConfig(name="agy", kind="cli", cli_command="agy -p"),
            ["first", "second"],
        )
        agent.send("turn one")
        agent.send("turn two")

        first_command, first_cwd = agent.commands[0]
        second_command, second_cwd = agent.commands[1]
        self.assertEqual(first_command[:2], ["agy", "-p"])
        self.assertIn("--continue", second_command)
        self.assertEqual(first_cwd, second_cwd)
        self.assertTrue(agent.provider_session_id().startswith("cwd:"))

    def test_duplicate_cli_agents_have_independent_sessions(self):
        config_a = AgentConfig(name="architect", role="architecture", kind="cli", cli_command="agy")
        config_b = AgentConfig(name="skeptic", role="risk review", kind="cli", cli_command="agy")
        first = FakeCLI(config_a, ["one"])
        second = FakeCLI(config_b, ["two"])
        first.send("hello")
        second.send("hello")
        self.assertNotEqual(first._session_cwd, second._session_cwd)
        self.assertNotEqual(first.provider_session_id(), second.provider_session_id())

    def test_orchestrator_has_no_seed_or_transition_generation(self):
        debate = """## DESIGN_APPEND
design
## PLAN_UPDATE
- [ ] build
## CONSENSUS_APPEND
ready
VOTE: AGREE
"""
        developer = """## FILE: src/app.py
print('ok')
## PLAN_UPDATE
- [x] build
"""
        reviewer = """## DESIGN_APPEND
reviewed
## VERDICT
APPROVE
"""
        tester = """## TEST_RESULTS_APPEND
passed
## PLAN_UPDATE
- [x] tested
## VERDICT
PASS
"""
        agent = StatefulFake(
            AgentConfig(name="solo", kind="openai", model="gpt-4o"),
            replies=[debate, developer, reviewer, tester],
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[agent],
                workspace=Workspace(directory),
                max_debate_rounds=1,
                max_build_iterations=1,
            )
            asyncio.run(orchestrator.run("build a tiny app"))

        self.assertEqual(len(agent.received), 4)
        self.assertIn("Product idea: build a tiny app", agent.received[0][0]["content"])
        self.assertNotIn("Wait for instructions", "\n".join(m.content for m in agent.history))

    def test_role_and_behavior_are_initialized_as_system_identity(self):
        agent = StatefulFake(
            AgentConfig(
                name="Ada", role="Security skeptic", kind="openai",
                model="gpt-4o", system_prompt="Challenge unsafe assumptions.",
            ),
            replies=["ok"],
        )
        orchestrator = Orchestrator([agent], Workspace(tempfile.mkdtemp()))
        orchestrator._running = True
        asyncio.run(orchestrator._send_agent(agent, "Review this"))
        self.assertIn("You are Ada", agent.received_systems[0])
        self.assertIn("Security skeptic", agent.received_systems[0])
        self.assertIn("Challenge unsafe assumptions", agent.received_systems[0])

    def test_rate_limit_is_retried_without_losing_the_logical_session(self):
        retry_events = []
        agent = RateLimitedFake(
            AgentConfig(name="retry", kind="openai", model="gpt-4o"),
            reply="recovered",
        )
        orchestrator = ImmediateRetryOrchestrator(
            [agent], Workspace(tempfile.mkdtemp()), event_cb=lambda event: retry_events.append(event)
        )
        orchestrator._running = True
        reply = asyncio.run(orchestrator._send_agent(agent, "try"))
        self.assertEqual(reply, "recovered")
        self.assertEqual(agent.attempts, 2)
        self.assertEqual(retry_events[0].kind.value, "retry")

    def test_workspace_writes_into_project_and_reads_agentflow_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            workspace.write_brief("Build a small service")
            workspace.init(workspace.brief())
            workspace.write_src("src/api/app.py", "print('ok')")

            self.assertEqual(workspace.brief().strip(), "Build a small service")
            self.assertEqual((workspace.project_root / "src/api/app.py").read_text(), "print('ok')")
            self.assertIn("src/api/app.py", workspace.read_src())
            self.assertNotIn(".agentflow/DESIGN.md", workspace.read_src())

    def test_sqlite_store_reuses_agents_and_run_history_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Workspace(directory)
            metadata.ensure()
            store = ProjectStore(metadata.root)
            store.save_agents([{
                "id": "agent-1", "name": "Builder", "role": "Developer",
                "kind": "openai", "model": "gpt-4o", "api_key": "secret",
                "cli_command": "", "system_prompt": "", "max_history_turns": 20,
                "extra": {},
            }])
            store.start_run("run-1", "Build it")
            store.append_event("run-1", {
                "timestamp": "now", "kind": "retry", "agent": "Builder", "data": {"attempt": 1},
            })
            store.finish_run("run-1", "done", [{"total_tokens": 42, "cost_usd": 0.01}])

            loaded = store.load_agents()
            runs = store.recent_runs()
            self.assertEqual(loaded[0]["role"], "Developer")
            self.assertEqual(loaded[0]["api_key"], "")
            self.assertEqual(runs[0]["total_tokens"], 42)
            self.assertEqual(runs[0]["status"], "done")
            store.close()


if __name__ == "__main__":
    unittest.main()
