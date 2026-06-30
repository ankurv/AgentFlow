"""Small per-project SQLite store for reusable AgentFlow state."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class ProjectStore:
    def __init__(self, metadata_dir: Path):
        metadata_dir.mkdir(parents=True, exist_ok=True)
        self.path = metadata_dir / "agentflow.db"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._db:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                """
            )

    def load_agents(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, config_json FROM agents ORDER BY sort_order"
            ).fetchall()
        configs = []
        for row in rows:
            config = json.loads(row["config_json"])
            config["id"] = row["id"]
            config["api_key"] = ""
            configs.append(config)
        return configs

    def save_agents(self, configs: list[dict]):
        with self._lock, self._db:
            self._db.execute("DELETE FROM agents")
            for index, original in enumerate(configs):
                config = dict(original)
                agent_id = config.pop("id")
                # Never write API credentials into a project-local database.
                config["api_key"] = ""
                self._db.execute(
                    "INSERT INTO agents(id, sort_order, config_json) VALUES (?, ?, ?)",
                    (agent_id, index, json.dumps(config)),
                )

    def start_run(self, run_id: str, idea: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs(run_id, idea, status, started_at) VALUES (?, ?, ?, ?)",
                (run_id, idea, "running", now),
            )

    def finish_run(self, run_id: str, status: str, agents: list[dict]):
        now = datetime.now(timezone.utc).isoformat()
        tokens = sum(int(agent.get("total_tokens", 0) or 0) for agent in agents)
        cost = sum(float(agent.get("cost_usd", 0) or 0) for agent in agents)
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs
                   SET status=?, completed_at=?, total_tokens=?, estimated_cost_usd=?
                   WHERE run_id=?""",
                (status, now, tokens, cost, run_id),
            )

    def append_event(self, run_id: str | None, event: dict):
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO events(run_id, timestamp, kind, agent, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    event.get("timestamp", ""),
                    event.get("kind", ""),
                    event.get("agent", ""),
                    json.dumps(event.get("data", {})),
                ),
            )

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT run_id, idea, status, started_at, completed_at,
                          total_tokens, estimated_cost_usd
                   FROM runs ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        with self._lock:
            self._db.close()
