"""FastAPI backend for project selection, orchestration, persistence, and SSE."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents.base import AgentConfig
from .agents.providers import AGENT_KINDS, create_agent
from .orchestrator import Event, EventKind, Orchestrator
from .storage import ProjectStore
from .workspace.workspace import Workspace
from .crypto import encrypt_key, decrypt_key

app = FastAPI(title="AgentFlow", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class AppState:
    def __init__(self):
        self.configs: list[dict] = []
        self.orchestrator: Optional[Orchestrator] = None
        self.workspace: Optional[Workspace] = None
        self.store: Optional[ProjectStore] = None
        self.event_log: list[dict] = []
        self.sse_clients: list[asyncio.Queue] = []
        self.run_id: Optional[str] = None
        self.status = "idle"
        self.current_idea = ""

    def open_project(self, path: str) -> Workspace:
        if self.status in {"running", "paused", "needs_attention"}:
            raise ValueError("Stop the active run before changing projects")
        workspace = Workspace(path)
        workspace.ensure()
        if self.store:
            self.store.close()
        self.workspace = workspace
        self.store = ProjectStore(workspace.root)
        self.configs = self.store.load_agents()
        self.event_log.clear()
        self.orchestrator = None
        self.run_id = None
        self.status = "idle"
        self.current_idea = workspace.brief()
        return workspace

    def persist_agents(self):
        if not self.workspace or not self.store:
            raise ValueError("Open a project first")
        self.store.save_agents(self.configs)

    @property
    def merged_configs(self) -> list[dict]:
        # Merge global and project configs by name (case-insensitive key). Local overrides global.
        merged = {cfg["name"].strip().lower(): cfg for cfg in load_global_agents()}
        for cfg in self.configs:
            merged[cfg["name"].strip().lower()] = cfg
        return list(merged.values())


state = AppState()

GLOBAL_AGENTS_PATH = Path.home() / ".agentflow" / "global_agents.json"

def load_global_agents() -> list[dict]:
    GLOBAL_AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_AGENTS_PATH.exists():
        return []
    try:
        configs = json.loads(GLOBAL_AGENTS_PATH.read_text())
        for c in configs:
            c["api_key"] = decrypt_key(c.get("api_key", ""))
        return configs
    except Exception:
        return []

def save_global_agents(configs: list[dict]):
    GLOBAL_AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    configs_copy = []
    for original in configs:
        c = dict(original)
        c["api_key"] = encrypt_key(c.get("api_key", ""))
        configs_copy.append(c)
    GLOBAL_AGENTS_PATH.write_text(json.dumps(configs_copy, indent=2))



def broadcast(event: Event):
    data = event.to_dict()
    if event.kind == EventKind.ERROR and event.data.get("recoverable"):
        state.status = "needs_attention"
    elif event.kind == EventKind.TURN_START and event.data.get("resumed"):
        state.status = "running"
    elif event.kind == EventKind.PHASE and event.data.get("status") in {"waiting_for_approval", "waiting_for_continuation"}:
        state.status = "paused"
    elif event.kind == EventKind.PHASE and event.data.get("status") == "continuing_debate":
        state.status = "running"
    state.event_log.append(data)
    if state.store:
        state.store.append_event(state.run_id, data)
    dead = []
    for queue in state.sse_clients:
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(queue)
    for queue in dead:
        state.sse_clients.remove(queue)


@app.get("/events")
async def sse_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    state.sse_clients.append(queue)

    async def generator():
        try:
            for past in state.event_log:
                yield f"data: {json.dumps(past)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in state.sse_clients:
                state.sse_clients.remove(queue)

    return StreamingResponse(
        generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ProjectOpenIn(BaseModel):
    path: str


class ProjectBriefIn(BaseModel):
    content: str


def project_payload() -> dict:
    if not state.workspace:
        return {"open": False, "path": "", "brief": "", "recent_runs": []}
    return {
        "open": True,
        "path": state.workspace.path,
        "brief": state.workspace.brief(),
        "recent_runs": state.store.recent_runs() if state.store else [],
    }


@app.get("/project")
def get_project():
    return project_payload()


@app.post("/project/open")
def open_project(body: ProjectOpenIn):
    try:
        state.open_project(body.path)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **project_payload(), "agents": state.configs}


@app.put("/project/brief")
def save_project_brief(body: ProjectBriefIn):
    if not state.workspace:
        raise HTTPException(400, "Open a project first")
    state.workspace.write_brief(body.content)
    return {"ok": True, "brief": state.workspace.brief()}


class AgentConfigIn(BaseModel):
    name: str
    kind: str
    role: str = ""
    model: str = ""
    api_key: str = ""
    cli_command: str = ""
    system_prompt: str = ""
    max_history_turns: int = 20
    extra: dict = Field(default_factory=dict)


def to_agent_config(config: dict) -> AgentConfig:
    return AgentConfig(
        id=config.get("id", ""), name=config["name"], kind=config["kind"],
        role=config.get("role", ""), model=config.get("model", ""),
        api_key=config.get("api_key", ""), cli_command=config.get("cli_command", ""),
        working_directory=state.workspace.path if state.workspace else "",
        system_prompt=config.get("system_prompt", ""),
        max_history_turns=config.get("max_history_turns", 20),
        extra=config.get("extra", {}),
    )


def live_agent(agent_id: str):
    if not state.orchestrator or state.status not in {"running", "paused", "needs_attention"}:
        return None
    return next(
        (agent for agent in state.orchestrator.agents if agent.config.id == agent_id),
        None,
    )


@app.get("/agents")
def list_agents():
    return {
        "global": load_global_agents(),
        "project": state.configs,
        "merged": state.merged_configs,
        "kinds": list(AGENT_KINDS.keys())
    }


@app.post("/agents")
def add_agent(body: AgentConfigIn):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before adding an agent")
    config = body.model_dump()
    config["id"] = str(uuid.uuid4())[:8]
    state.configs.append(config)
    state.persist_agents()
    return {"ok": True, "agent": config}


@app.put("/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentConfigIn):
    for index, current in enumerate(state.configs):
        if current["id"] == agent_id:
            updated = body.model_dump()
            updated["id"] = agent_id
            active = live_agent(agent_id)
            if active:
                if updated["kind"] != current["kind"] or updated["name"] != current["name"]:
                    raise HTTPException(
                        400, "An active agent's name and kind cannot change; stop the run first"
                    )
                try:
                    active.reconfigure(to_agent_config(updated))
                except Exception as exc:
                    raise HTTPException(400, f"Agent configuration is invalid: {exc}") from exc
            state.configs[index] = updated
            state.persist_agents()
            return {"ok": True, "agent": updated}
    raise HTTPException(404, "Agent not found")


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "Stop the active run before removing an agent")
    state.configs = [config for config in state.configs if config["id"] != agent_id]
    state.persist_agents()
    return {"ok": True}


@app.get("/agents/global")
def list_global_agents():
    return {"agents": load_global_agents()}


@app.post("/agents/global")
def add_global_agent(body: AgentConfigIn):
    configs = load_global_agents()
    config = body.model_dump()
    config["id"] = str(uuid.uuid4())[:8]
    configs.append(config)
    save_global_agents(configs)
    return {"ok": True, "agent": config}


@app.delete("/agents/global/{agent_id}")
def delete_global_agent(agent_id: str):
    configs = load_global_agents()
    configs = [c for c in configs if c["id"] != agent_id]
    save_global_agents(configs)
    return {"ok": True}


@app.put("/agents/global/{agent_id}")
def update_global_agent(agent_id: str, body: AgentConfigIn):
    configs = load_global_agents()
    for index, current in enumerate(configs):
        if current["id"] == agent_id:
            updated = body.model_dump()
            updated["id"] = agent_id
            
            active = live_agent(agent_id)
            if active:
                if updated["kind"] != current["kind"] or updated["name"] != current["name"]:
                    raise HTTPException(
                        400, "An active agent's name and kind cannot change; stop the run first"
                    )
                try:
                    active.reconfigure(to_agent_config(updated))
                except Exception as exc:
                    raise HTTPException(400, f"Agent configuration is invalid: {exc}") from exc
                    
            configs[index] = updated
            save_global_agents(configs)
            return {"ok": True, "agent": updated}
    raise HTTPException(404, "Global agent not found")


@app.post("/agents/test")
def test_agent_config(body: AgentConfigIn):
    try:
        config = to_agent_config(body.model_dump())
        agent = create_agent(config)
        agent.send("ping")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class StartBody(BaseModel):
    idea: str = ""
    project_path: str = ""
    save_brief: bool = False
    max_debate_rounds: int = 6
    max_build_iterations: int = 5
    mode: str = "all"


@app.post("/run/start")
async def start_run(body: StartBody):
    if state.status in {"running", "paused", "needs_attention"}:
        raise HTTPException(400, "A run is already in progress")
    if body.project_path:
        requested = str(Path(body.project_path).expanduser().resolve())
        if not state.workspace or state.workspace.path != requested:
            try:
                state.open_project(requested)
            except (OSError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
    if not state.workspace:
        raise HTTPException(400, "Open a project folder first")
    if not state.merged_configs:
        raise HTTPException(400, "No agents configured")
    names = [config["name"].strip() for config in state.merged_configs]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise HTTPException(400, "Every agent needs a unique non-empty name")

    idea = body.idea.strip() or state.workspace.brief().strip()
    if not idea:
        raise HTTPException(400, "Describe what to build or add AGENTFLOW.md to the project")
    if body.save_brief and body.idea.strip():
        state.workspace.write_brief(body.idea)

    agents = []
    try:
        for config in state.merged_configs:
            agents.append(create_agent(to_agent_config(config)))
    except Exception as exc:
        raise HTTPException(400, f"Could not initialize agent team: {exc}") from exc

    state.event_log.clear()
    state.run_id = str(uuid.uuid4())[:8]
    state.current_idea = idea
    state.status = "running"
    if state.store:
        state.store.start_run(state.run_id, idea)

    state.orchestrator = Orchestrator(
        agents=agents,
        workspace=state.workspace,
        event_cb=broadcast,
        max_debate_rounds=body.max_debate_rounds,
        max_build_iterations=body.max_build_iterations,
        require_approval=True,
        mode=body.mode,
    )

    async def run_and_update():
        try:
            snapshot = await state.orchestrator.run(idea)
            if state.status != "idle":
                state.status = "done"
                if state.store and state.run_id:
                    state.store.finish_run(
                        state.run_id, "done",
                        [agent.state_dict() for agent in state.orchestrator.agents],
                    )
                broadcast(Event(kind=EventKind.DONE, data={"workspace": snapshot or {}}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            state.status = "error"
            broadcast(Event(kind=EventKind.ERROR, data={"error": str(exc)}))
            if state.store and state.run_id:
                state.store.finish_run(
                    state.run_id, "error",
                    [agent.state_dict() for agent in state.orchestrator.agents],
                )

    asyncio.create_task(run_and_update())
    return {"ok": True, "run_id": state.run_id, "idea_source": "prompt" if body.idea.strip() else "AGENTFLOW.md"}


@app.post("/run/pause")
def pause_run():
    if state.status == "needs_attention":
        raise HTTPException(409, "Fix the failed agent and retry its turn")
    if state.orchestrator:
        state.orchestrator.pause()
        state.status = "paused"
    return {"ok": True, "status": state.status}


@app.post("/run/resume")
def resume_run():
    if state.orchestrator and state.orchestrator.failed_turn:
        raise HTTPException(409, "Use Retry failed turn after fixing the agent")
    if state.orchestrator:
        state.orchestrator.resume()
        state.status = "running"
    return {"ok": True, "status": state.status}


@app.post("/run/retry")
def retry_failed_turn():
    if not state.orchestrator or not state.orchestrator.failed_turn:
        raise HTTPException(400, "There is no failed turn to retry")
    state.orchestrator.retry_failed_turn()
    state.status = "running"
    return {"ok": True, "status": state.status,
            "turn": state.orchestrator.failed_turn}


@app.post("/run/stop")
def stop_run():
    if state.orchestrator:
        state.orchestrator.stop()
        state.orchestrator.resume()
        if state.store and state.run_id:
            state.store.finish_run(
                state.run_id, "stopped",
                [agent.state_dict() for agent in state.orchestrator.agents],
            )
    state.status = "idle"
    return {"ok": True}


class SteerBody(BaseModel):
    message: str


@app.post("/run/steer")
async def steer_run(body: SteerBody):
    if not state.orchestrator:
        raise HTTPException(400, "No active run")
    await state.orchestrator.steer(body.message)
    return {"ok": True}


@app.get("/run/status")
def run_status():
    agents = [agent.state_dict() for agent in state.orchestrator.agents] if state.orchestrator else []
    return {
        "status": state.status,
        "run_id": state.run_id,
        "idea": state.current_idea,
        "project_path": state.workspace.path if state.workspace else "",
        "agents": agents,
        "failed_turn": state.orchestrator.failed_turn if state.orchestrator else None,
    }


@app.get("/runs")
def recent_runs():
    return {"runs": state.store.recent_runs() if state.store else []}


@app.get("/runs/{run_id}/turns")
def run_turns(run_id: str):
    return {"turns": state.store.run_turns(run_id) if state.store else []}


@app.get("/workspace")
def get_workspace():
    if not state.workspace:
        return {"project_path": "", "src": {}, "src_files": []}
    return state.workspace.snapshot()


@app.get("/workspace/file/{key}")
def get_file(key: str):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    allowed = ["design", "plan", "consensus", "tests", "questions"]
    if key not in allowed:
        raise HTTPException(400, f"key must be one of {allowed}")
    return {"key": key, "content": state.workspace.read(key)}


class FileUpdateBody(BaseModel):
    content: str

@app.post("/workspace/file/{key}")
def update_file(key: str, body: FileUpdateBody):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    allowed = ["design", "plan", "consensus", "tests", "questions"]
    if key not in allowed:
        raise HTTPException(400, f"key must be one of {allowed}")
    state.workspace.write(key, body.content)
    return {"ok": True}


@app.get("/workspace/src/{filename:path}")
def get_src_file(filename: str):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    src = state.workspace.read_src()
    if filename not in src:
        raise HTTPException(404, "File not found")
    return {"filename": filename, "content": src[filename]}


@app.post("/workspace/src/{filename:path}")
def update_src_file(filename: str, body: FileUpdateBody):
    if not state.workspace:
        raise HTTPException(404, "No active workspace")
    try:
        state.workspace.write_src(filename, body.content)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/events/history")
def event_history():
    return {"events": state.event_log}


_frontend = Path(__file__).parent.parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
