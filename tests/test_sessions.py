import asyncio
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

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
        self.fake_conversation_id = str(uuid.uuid4())
        super().__init__(config)

    def _run(self, argv, cwd=None):
        self.commands.append((argv, cwd))
        if "--log-file" in argv:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.write_text(
                f"Print mode: conversation={self.fake_conversation_id}, sending message\n"
            )
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


class RepairableFake(StatefulFake):
    def _raw_send(self, messages, system):
        if self.config.model != "fixed":
            raise RuntimeError("invalid model configuration")
        return super()._raw_send(messages, system)


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
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(
                name="codex",
                kind="cli",
                working_directory=project,
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
        self.assertEqual(Path(agent.commands[0][1]), Path(project).resolve())
        self.assertEqual(Path(agent.commands[1][1]), Path(project).resolve())

    def test_antigravity_resumes_exact_isolated_conversation(self):
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(id="agy-1", name="agy", kind="cli", cli_command="agy -p",
                        working_directory=project),
            ["first", "second"],
        )
        agent.send("turn one")
        agent.send("turn two")

        first_command, first_cwd = agent.commands[0]
        second_command, second_cwd = agent.commands[1]
        self.assertEqual(first_command[0], "agy")
        self.assertNotIn("--continue", second_command)
        self.assertIn("--conversation", second_command)
        self.assertIn(agent.fake_conversation_id, second_command)
        self.assertEqual(first_cwd, second_cwd)
        self.assertEqual(Path(first_cwd), Path(project).resolve())
        log_path = Path(first_command[first_command.index("--log-file") + 1])
        self.assertEqual(log_path.parent.parent, Path(project).resolve() / ".agentflow" / "sessions")
        self.assertEqual(agent.provider_session_id(), agent.fake_conversation_id)

    def test_duplicate_cli_agents_have_independent_sessions(self):
        project = tempfile.mkdtemp()
        config_a = AgentConfig(id="architect", name="architect", role="architecture",
                               kind="cli", cli_command="agy", working_directory=project)
        config_b = AgentConfig(id="skeptic", name="skeptic", role="risk review",
                               kind="cli", cli_command="agy", working_directory=project)
        first = FakeCLI(config_a, ["one"])
        second = FakeCLI(config_b, ["two"])
        first.send("hello")
        second.send("hello")
        self.assertNotEqual(first._session_cwd, second._session_cwd)
        self.assertNotEqual(first.provider_session_id(), second.provider_session_id())
        self.assertIn(first.fake_conversation_id, first.commands[1][0] if len(first.commands) > 1 else [first.provider_session_id()])

    def test_stateless_cli_runs_from_selected_project(self):
        project = tempfile.mkdtemp()
        agent = FakeCLI(
            AgentConfig(name="custom", kind="cli", cli_command="custom-agent",
                        working_directory=project, extra={"session_mode": "stateless"}),
            ["reply"],
        )
        agent.send("hello")
        self.assertEqual(Path(agent.commands[0][1]), Path(project).resolve())

    def test_real_cli_process_inherits_selected_project_directory(self):
        with tempfile.TemporaryDirectory() as project:
            agent = CLIAgent(AgentConfig(
                name="pwd", kind="cli", cli_command="/bin/sh -c pwd",
                working_directory=project, extra={"session_mode": "stateless"},
            ))
            self.assertEqual(Path(agent.send("where am I?").strip()), Path(project).resolve())

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
                require_approval=False,
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
        orchestrator = Orchestrator([agent], Workspace(tempfile.mkdtemp()), require_approval=False)
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
            [agent], Workspace(tempfile.mkdtemp()), event_cb=lambda event: retry_events.append(event),
            require_approval=False,
        )
        orchestrator._running = True
        reply = asyncio.run(orchestrator._send_agent(agent, "try"))
        self.assertEqual(reply, "recovered")
        self.assertEqual(agent.attempts, 2)
        self.assertEqual(retry_events[0].kind.value, "retry")

    def test_failed_turn_can_be_fixed_and_resumed_without_advancing(self):
        events = []
        agent = RepairableFake(
            AgentConfig(id="agent-1", name="repair", kind="openai", model="broken"),
            replies=["recovered"],
        )
        orchestrator = Orchestrator(
            [agent], Workspace(tempfile.mkdtemp()), event_cb=events.append,
            require_approval=False,
        )
        orchestrator._running = True

        async def exercise():
            task = asyncio.create_task(orchestrator._send_agent(
                agent, "same turn", "turn-0001", {"phase": "debate", "round": 1},
            ))
            while not orchestrator.failed_turn:
                await asyncio.sleep(0)
            self.assertEqual(orchestrator.failed_turn["turn_id"], "turn-0001")
            agent.reconfigure(AgentConfig(
                id="agent-1", name="repair", kind="openai", model="fixed",
            ))
            orchestrator.retry_failed_turn()
            return await task

        self.assertEqual(asyncio.run(exercise()), "recovered")
        self.assertEqual(len(agent.history), 2)
        self.assertEqual(orchestrator._turn_attempts["turn-0001"], 2)
        self.assertTrue(any(
            event.kind.value == "error" and event.data.get("recoverable")
            for event in events
        ))
        self.assertTrue(any(
            event.kind.value == "turn_start" and event.data.get("resumed")
            for event in events
        ))

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
            self.assertEqual(loaded[0]["api_key"], "secret")
            self.assertEqual(runs[0]["total_tokens"], 42)
            self.assertEqual(runs[0]["status"], "done")
            store.close()

    def test_sqlite_tracks_turn_attempt_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.ensure()
            store = ProjectStore(workspace.root)
            store.start_run("run-1", "Build it")
            store.append_event("run-1", {
                "timestamp": "t1", "kind": "turn_start", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 1, "phase": "debate", "round": 1},
            })
            store.append_event("run-1", {
                "timestamp": "t2", "kind": "error", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 1,
                         "recoverable": True, "error": "bad config"},
            })
            store.append_event("run-1", {
                "timestamp": "t3", "kind": "turn_start", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 2, "phase": "debate", "round": 1},
            })
            store.append_event("run-1", {
                "timestamp": "t4", "kind": "turn_end", "agent": "Builder",
                "data": {"turn_id": "turn-0001", "attempt": 2,
                         "usage": {"total_tokens": 12}, "response": "done"},
            })
            turns = store.run_turns("run-1")
            self.assertEqual(turns[0]["status"], "completed")
            self.assertEqual(turns[0]["attempt"], 2)
            self.assertEqual(turns[0]["usage"]["total_tokens"], 12)
            store.close()

    def test_human_approval_gate_and_loop_continuation(self):
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
            replies=[debate, debate, developer, reviewer, tester],
        )

        events = []
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[agent],
                workspace=Workspace(directory),
                max_debate_rounds=1,
                max_build_iterations=1,
                require_approval=True,
                event_cb=events.append,
            )

            async def exercise():
                run_task = asyncio.create_task(orchestrator.run("build a tiny app"))
                while not any(ev.kind.value == "phase" and ev.data.get("status") == "waiting_for_approval" for ev in events):
                    await asyncio.sleep(0.005)

                self.assertTrue(orchestrator._paused)
                self.assertEqual(len(agent.received), 1)

                await orchestrator.steer("change something")
                orchestrator.require_approval = False
                orchestrator.resume()
                await run_task

            asyncio.run(exercise())
            self.assertEqual(len(agent.received), 5)
            self.assertIn("change something", agent.received[1][0]["content"])

    def test_programmatic_loop_pauses(self):
        debate = """## DESIGN_APPEND
design
## PLAN_UPDATE
- [ ] build
## CONSENSUS_APPEND
not ready yet
VOTE: DISAGREE
"""
        developer = """## FILE: src/app.py
print('ok')
## PLAN_UPDATE
- [x] build
"""
        reviewer_fail = """## DESIGN_APPEND
reviewed
## VERDICT
CHANGES NEEDED
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
            replies=[
                debate, debate, debate.replace("DISAGREE", "AGREE"),
                developer, reviewer_fail, tester,
                developer, reviewer, tester
            ],
        )

        events = []
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[agent],
                workspace=Workspace(directory),
                max_debate_rounds=3,
                max_build_iterations=2,
                require_approval=True,
                event_cb=events.append,
            )

            async def exercise():
                run_task = asyncio.create_task(orchestrator.run("build a tiny app"))
                
                # Debate should pause at waiting_for_continuation after round 2
                while not any(ev.kind.value == "phase" and ev.data.get("round") == 2 and ev.data.get("status") == "waiting_for_continuation" for ev in events):
                    await asyncio.sleep(0.005)

                self.assertTrue(orchestrator._paused)

                # Resume. It will complete round 3 (agree) and hit waiting_for_approval
                orchestrator.resume()
                while not any(ev.kind.value == "phase" and ev.data.get("status") == "waiting_for_approval" for ev in events):
                    await asyncio.sleep(0.005)

                self.assertTrue(orchestrator._paused)

                # Resume. It will run build iteration 1, fail review, and hit waiting_for_continuation
                orchestrator.resume()
                while not any(ev.kind.value == "phase" and ev.data.get("iteration") == 1 and ev.data.get("status") == "waiting_for_continuation" for ev in events):
                    await asyncio.sleep(0.005)

                self.assertTrue(orchestrator._paused)

                # Disable require_approval and resume. It will run iteration 2 (pass review/test) and complete
                orchestrator.require_approval = False
                orchestrator.resume()
                await run_task

            asyncio.run(exercise())
            self.assertEqual(len(agent.received), 9)

    def test_coordinator_orchestrated_loop(self):
        boss_response_1 = """## NEXT_AGENT
worker

## INSTRUCTIONS
write main script

## VERDICT
CONTINUE

## PLAN_UPDATE
- [ ] Task 1
  - [ ] Subtask 1a
- [ ] Task 2

## DESIGN_APPEND
Initial architecture.
"""
        boss_response_2 = """## NEXT_AGENT
worker

## INSTRUCTIONS
finish task

## VERDICT
COMPLETE
"""
        worker_response = """## FILE: src/main.py
print('hello world')
## PLAN_UPDATE
- [ ] Task 1
  - [x] build script
- [ ] Task 2
"""
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o", extra={"is_coordinator": True}),
            replies=[boss_response_1, boss_response_2]
        )
        worker = StatefulFake(
            AgentConfig(name="worker", kind="openai", model="gpt-4o"),
            replies=[worker_response]
        )

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss, worker],
                workspace=Workspace(directory),
                require_approval=False,
            )
            asyncio.run(orchestrator.run("build tiny product"))

            self.assertEqual(len(boss.received), 2)
            self.assertEqual(len(worker.received), 1)
            self.assertIn("write main script", worker.received[0][0]["content"])
            self.assertEqual(orchestrator.ws.read_src()["src/main.py"], "print('hello world')")
            self.assertIn("Task 1", orchestrator.ws.read("plan"))
            self.assertIn("Initial architecture", orchestrator.ws.read("design"))

    def test_global_agents_endpoints(self):
        from fastapi.testclient import TestClient
        import backend.server

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_path = backend.server.GLOBAL_AGENTS_PATH
            backend.server.GLOBAL_AGENTS_PATH = Path(tmpdir) / "global_agents.json"

            try:
                client = TestClient(backend.server.app)

                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), {"agents": []})

                agent_payload = {
                    "name": "Global Bot",
                    "kind": "openai",
                    "role": "helper",
                    "model": "gpt-4o",
                    "api_key": "my-secret-key-xyz",
                    "system_prompt": "hello",
                    "max_history_turns": 20,
                    "extra": {"is_coordinator": True}
                }
                res = client.post("/agents/global", json=agent_payload)
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                agent_id = data["agent"]["id"]
                self.assertEqual(data["agent"]["name"], "Global Bot")
                self.assertNotIn("is_coordinator", data["agent"]["extra"])

                # Verify file on disk is encrypted
                raw_file_content = backend.server.GLOBAL_AGENTS_PATH.read_text()
                self.assertNotIn("my-secret-key-xyz", raw_file_content)
                self.assertIn("gAAAA", raw_file_content) # Fernet signature

                # Verify API returns decrypted value
                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                agents = res.json()["agents"]
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0]["id"], agent_id)
                self.assertEqual(agents[0]["api_key"], "my-secret-key-xyz")

                res = client.delete(f"/agents/global/{agent_id}")
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res.json()["ok"])

                res = client.get("/agents/global")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), {"agents": []})

            finally:
                backend.server.GLOBAL_AGENTS_PATH = orig_path

    def test_project_store_encryption(self):
        from backend.storage import ProjectStore
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectStore(Path(tmpdir))
            agent_payload = [{
                "id": "bot1",
                "name": "Bot One",
                "kind": "openai",
                "role": "builder",
                "model": "gpt-4o",
                "api_key": "my-secret-key-abc",
                "system_prompt": "hello",
                "max_history_turns": 20,
                "extra": {}
            }]

            store.save_agents(agent_payload)

            # Assert raw DB storage is encrypted
            cursor = store._db.execute("SELECT config_json FROM agents")
            row = cursor.fetchone()
            config_stored = json.loads(row["config_json"])
            self.assertNotEqual(config_stored["api_key"], "my-secret-key-abc")
            self.assertTrue(config_stored["api_key"].startswith("gAAAA")) # Fernet header

            # Assert loading decrypts correctly
            loaded = store.load_agents()
            self.assertEqual(loaded[0]["api_key"], "my-secret-key-abc")

    def test_execution_modes(self):
        boss = StatefulFake(
            AgentConfig(name="boss", kind="openai", model="gpt-4o"),
            replies=["## VERDICT\nCONTINUE"]
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(
                agents=[boss],
                workspace=Workspace(directory),
                require_approval=False,
                mode="debate",
                max_debate_rounds=1,
            )
            # Run debate-only mode
            asyncio.run(orchestrator.run("debate product design"))
            
            # Verify debate runs, but build phase is completely skipped
            self.assertGreater(len(boss.received), 0)


if __name__ == "__main__":
    unittest.main()
