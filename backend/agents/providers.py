"""Provider adapters with native logical sessions where available."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .base import AgentBase, AgentConfig, Usage


class ClaudeAgent(AgentBase):
    def __init__(self, config: AgentConfig):
        super().__init__(config)
        import anthropic

        key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        # Claude's Messages API is stateless, so AgentBase supplies a bounded
        # history. Cache the stable system prefix when one is present.
        system_value = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if system else ""
        )
        response = self._client.messages.create(
            model=self.config.model or "claude-sonnet-4-6",
            max_tokens=self.config.extra.get("max_tokens", 2000),
            system=system_value,
            messages=messages,
        )
        text = response.content[0].text
        raw = response.usage
        cached = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        usage = Usage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0) + cached + cache_write,
            cached_input_tokens=cached,
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        )
        return text, usage


class OpenAIAgent(AgentBase):
    """Responses API adapter chained with previous_response_id."""

    manages_context = True

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        import openai

        key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = openai.OpenAI(api_key=key) if key else openai.OpenAI()
        self._response_id: str | None = None

    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        kwargs = {
            "model": self.config.model or "gpt-4o",
            "input": messages[-1]["content"],
            "instructions": system or None,
            "max_output_tokens": self.config.extra.get("max_tokens", 2000),
            "previous_response_id": self._response_id,
            "store": True,
            "prompt_cache_key": f"agentflow-{self._session_id}",
        }
        compact_threshold = int(self.config.extra.get("compact_threshold", 0) or 0)
        if compact_threshold:
            kwargs["context_management"] = [{
                "type": "compaction",
                "compact_threshold": compact_threshold,
            }]
        response = self._client.responses.create(**kwargs)
        self._response_id = response.id
        raw = response.usage
        details = getattr(raw, "input_tokens_details", None)
        usage = Usage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        )
        return response.output_text, usage

    def _reset_provider_session(self):
        self._response_id = None

    def provider_session_id(self) -> str:
        return self._response_id or ""


class GeminiAgent(AgentBase):
    """Retains the provider's ChatSession and sends only the new user turn."""

    manages_context = True

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        import google.generativeai as genai

        key = config.api_key or os.environ.get("GEMINI_API_KEY", "")
        if key:
            genai.configure(api_key=key)
        self._genai = genai
        self._model_name = config.model or "gemini-2.5-flash"
        self._chat = None

    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        if self._chat is None:
            model = self._genai.GenerativeModel(
                self._model_name,
                system_instruction=system or None,
            )
            self._chat = model.start_chat(history=[])
        response = self._chat.send_message(messages[-1]["content"])
        raw = getattr(response, "usage_metadata", None)
        usage = Usage(
            input_tokens=int(getattr(raw, "prompt_token_count", 0) or 0),
            cached_input_tokens=int(getattr(raw, "cached_content_token_count", 0) or 0),
            output_tokens=int(getattr(raw, "candidates_token_count", 0) or 0),
        )
        return response.text, usage

    def _reset_provider_session(self):
        self._chat = None

    def provider_session_id(self) -> str:
        return self._session_id if self._chat is not None else ""


class CLIAgent(AgentBase):
    """CLI adapter with resumable Codex and Antigravity conversations."""

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._argv = shlex.split(config.cli_command)
        if not self._argv:
            raise ValueError(f"CLIAgent '{self.name}' has no cli_command configured")
        requested = str(config.extra.get("session_mode", "auto")).lower()
        command = Path(self._argv[0]).name.lower()
        joined = " ".join(self._argv).lower()
        if requested != "auto":
            self._session_mode = requested
        elif "codex" in command or re.search(r"\bcodex\s+exec\b", joined):
            self._session_mode = "codex"
        elif command in {"agy", "antigravity"} or "antigravity" in command:
            self._session_mode = "antigravity"
        else:
            self._session_mode = "stateless"
        self.manages_context = self._session_mode in {"codex", "antigravity"}
        self._provider_session_id = ""
        self._session_cwd = tempfile.mkdtemp(prefix=f"agentflow-{self._session_id}-")

    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        if self._session_mode == "codex":
            return self._send_codex(messages[-1]["content"], system)
        if self._session_mode == "antigravity":
            return self._send_antigravity(messages[-1]["content"], system)
        return self._send_stateless(messages, system)

    def _run(self, argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=int(self.config.extra.get("timeout", 300)),
            cwd=cwd,
        )
        if result.returncode != 0 and not result.stdout:
            raise RuntimeError(result.stderr.strip() or f"CLI exited {result.returncode}")
        return result

    def _initial_prompt(self, message: str, system: str) -> str:
        if system:
            return f"[SYSTEM]\n{system}\n\n[USER]\n{message}"
        return message

    def _codex_parts(self) -> tuple[list[str], list[str]]:
        args = [arg for arg in self._argv if arg not in {"--json", "--ephemeral"}]
        try:
            idx = args.index("exec")
        except ValueError:
            return args + ["exec"], []
        return args[:idx + 1], args[idx + 1:]

    def _send_codex(self, message: str, system: str) -> tuple[str, Usage]:
        prefix, options = self._codex_parts()
        prompt = self._initial_prompt(message, system) if not self._provider_session_id else message
        if self._provider_session_id:
            argv = prefix + ["resume"] + options + ["--json", self._provider_session_id, prompt]
        else:
            argv = prefix + options + ["--json", prompt]
        result = self._run(argv)

        text = ""
        usage = Usage(estimated=True)
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                self._provider_session_id = event.get("thread_id", "")
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", text)
            if event.get("type") == "turn.completed":
                raw = event.get("usage", {})
                usage = Usage(
                    input_tokens=int(raw.get("input_tokens", 0) or 0),
                    cached_input_tokens=int(raw.get("cached_input_tokens", 0) or 0),
                    output_tokens=int(raw.get("output_tokens", 0) or 0),
                )
        if not text:
            text = result.stdout.strip()
        if usage.total_tokens == 0:
            usage = self._estimated_usage(prompt, text)
        return text, usage

    def _antigravity_base_args(self) -> list[str]:
        args = list(self._argv)
        # AgentFlow supplies the prompt, so a trailing prompt flag belongs to
        # the adapter rather than the configured base command.
        if args and args[-1] in {"-p", "--prompt"}:
            args.pop()
        return args

    def _send_antigravity(self, message: str, system: str) -> tuple[str, Usage]:
        args = self._antigravity_base_args()
        prompt = self._initial_prompt(message, system) if not self._provider_session_id else message
        if self._provider_session_id and not self._provider_session_id.startswith("cwd:"):
            args += ["--conversation", self._provider_session_id]
        elif self._provider_session_id.startswith("cwd:"):
            args += ["--continue"]
        result = self._run(args + ["-p", prompt], cwd=self._session_cwd)
        combined = f"{result.stdout}\n{result.stderr}"
        match = re.search(
            r"(?:conversation|session)[^0-9a-f]*([0-9a-f]{8}-[0-9a-f-]{27,})",
            combined,
            re.IGNORECASE,
        )
        if match:
            self._provider_session_id = match.group(1)
        elif not self._provider_session_id:
            # Conversation histories are scoped to cwd; --continue is safe
            # because every AgentFlow agent owns a unique session directory.
            self._provider_session_id = f"cwd:{self._session_cwd}"
        text = result.stdout.strip()
        return text, self._estimated_usage(prompt, text)

    def _send_stateless(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        parts = [f"[SYSTEM]\n{system}\n"] if system else []
        for message in messages:
            label = "USER" if message["role"] == "user" else "ASSISTANT"
            parts.append(f"[{label}]\n{message['content']}")
        parts.append("[ASSISTANT]")
        prompt = "\n\n".join(parts)
        result = self._run(self._argv + [prompt])
        text = result.stdout.strip()
        return text, self._estimated_usage(prompt, text)

    @staticmethod
    def _estimated_usage(prompt: str, text: str) -> Usage:
        # Better than the old output-word count while remaining clearly marked
        # as an estimate when a CLI does not expose provider usage metadata.
        return Usage(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            estimated=True,
        )

    def _reset_provider_session(self):
        self._provider_session_id = ""

    def provider_session_id(self) -> str:
        return self._provider_session_id


class OllamaAgent(AgentBase):
    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._base_url = config.extra.get("base_url", "http://localhost:11434")

    def _raw_send(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        import urllib.request

        payload = {
            "model": self.config.model or "llama3",
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read())
        text = data["message"]["content"]
        usage = Usage(
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )
        return text, usage


AGENT_KINDS: dict[str, type[AgentBase]] = {
    "claude": ClaudeAgent,
    "openai": OpenAIAgent,
    "gemini": GeminiAgent,
    "cli": CLIAgent,
    "ollama": OllamaAgent,
}


def create_agent(config: AgentConfig) -> AgentBase:
    cls = AGENT_KINDS.get(config.kind)
    if not cls:
        raise ValueError(f"Unknown agent kind '{config.kind}'. Options: {list(AGENT_KINDS)}")
    return cls(config)
