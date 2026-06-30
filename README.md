# AgentFlow

Multi-agent debate + build loop with a live monitoring UI.

## Setup

```bash
cd AGENTFLOW
python3 -m pip install -r requirements.txt
python3 run.py
# open http://localhost:8765
```

## Project folders and `AGENTFLOW.md`

Enter an absolute project-folder path in the UI and choose **Open / Create**. AgentFlow
creates the folder when needed and writes generated source files directly into it.
Orchestration-only files and the local database live under `.agentflow/`, which is
created automatically and ignored by Git.

The project brief can be typed in the UI, or saved as `AGENTFLOW.md` in the project
root. When the brief field is empty, **Start run** automatically uses that file. This
makes a project self-describing and reusable across AgentFlow restarts.

Example:

```markdown
# AGENTFLOW.md

Build a FastAPI service with `/health`, SQLite persistence, tests, and a Dockerfile.
Prefer a small dependency surface and document local development commands.
```

## Structure

```
agentflow/
├── backend/
│   ├── agents/
│   │   ├── base.py         # AgentBase — abstract session with sliding-window memory
│   │   └── providers.py    # Claude, OpenAI, Gemini, CLI, Ollama implementations
│   ├── workspace/
│   │   └── workspace.py    # Project-root file store with diff-based context
│   ├── orchestrator.py     # Debate + build phases, SSE events, human steering
│   ├── storage.py          # Per-project SQLite agents, runs, and events
│   └── server.py           # FastAPI — REST + SSE
├── frontend/
│   └── index.html          # Single-page UI
├── run.py
└── requirements.txt
```

## Adding a new agent provider

Subclass `AgentBase` and implement `_raw_send`:

```python
from backend.agents.base import AgentBase, AgentConfig

class MyAgent(AgentBase):
    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, int]:
        # call your model/API/CLI here
        response_text = my_api.call(system, messages)
        token_count = len(response_text.split())
        return response_text, token_count

# Register it
from backend.agents.providers import AGENT_KINDS
AGENT_KINDS["myagent"] = MyAgent
```

## Token optimization

- OpenAI API agents use the Responses API and retain `previous_response_id`
- Codex CLI agents retain their exact Codex thread ID and resume it each turn
- Antigravity CLI agents retain an isolated, workspace-scoped conversation
- Stateful providers receive only the new turn; stateless providers use a configurable sliding window
- `changed_context()` sends only files that changed since the agent's last turn
- Role-based filtering: developer only sees plan+src, reviewer sees design+src, etc.
- Seed and phase-transition instructions are folded into useful turns instead of spending extra calls
- Stable prompt prefixes allow provider prompt caches to reduce repeated-input cost

Each logical agent owns its own provider session. Two agents may therefore use the
same CLI command while retaining independent conversations, names, roles, and behavior
prompts. For example, duplicate a Codex agent and configure one as an architecture
lead and the other as a skeptical reviewer. The role and system behavior are installed
when each session starts and remain part of that agent's identity.

Stateful support currently includes exact Codex thread resume, workspace-isolated
Antigravity continuation, OpenAI Responses `previous_response_id`, and Gemini chat
sessions. Arbitrary CLI commands use stateless mode because AgentFlow cannot infer a
safe vendor-specific resume protocol for them.

## Usage and cost tracking

The live sidebar shows input, cached-input, output, and total tokens per agent.
The footer shows aggregate tokens and estimated USD cost. Known default model prices
are included for the built-in default API models. You can override input, cached-input,
and output USD-per-million-token rates on each agent card.

CLI subscription usage and local Ollama usage do not have a reliable per-token dollar
price, so the UI displays them as unpriced unless you configure explicit rates.

For authenticated CLI sessions, use the `cli` kind and leave the API key blank:

```text
Codex:      codex exec --skip-git-repo-check
Antigravity: /Users/you/.local/bin/agy
```

Choose `auto` session mode to detect Codex or Antigravity from the command, or select
the mode explicitly. Do not add `--ephemeral` to Codex; AgentFlow removes it because
ephemeral threads cannot be resumed.

## Persistence and automatic retries

Each project stores agent configuration, run summaries, and event history in
`.agentflow/agentflow.db`. API keys are deliberately stripped before persistence and
remain memory-only. Reopening the same project restores its agents and run history.

When a provider reports a usage limit, quota limit, HTTP 429, or a recognizable retry
time, the affected agent enters a **waiting** state. The UI displays the reason and
retry time; AgentFlow sleeps until that time and retries the same turn without adding
duplicate conversation history. If no reset time is available, it uses bounded
exponential backoff. Retry base and maximum wait are configurable per agent, and the
run can still be stopped while waiting.

## Human steering

While a run is active, type in the steer bar at the bottom of the Live Feed tab.
Your message is injected into the next turn for all agents to see.
You can also Pause/Resume to inspect the workspace before letting agents continue.
