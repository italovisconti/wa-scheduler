from __future__ import annotations

import fcntl
import json
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from wa_scheduler.config import Settings, get_settings


class WacliError(RuntimeError):
    pass


@dataclass
class WacliCommandResult:
    stdout: str
    stderr: str
    command: list[str]


class WacliClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _base_command(self) -> list[str]:
        return [
            self.settings.wacli_bin,
            "--json",
            "--store",
            str(self.settings.wacli_store_dir),
        ]

    @contextmanager
    def _execution_lock(self):
        # Shared by the web process and the worker so only one command touches the
        # wacli store at a time.
        self.settings.wacli_execution_lock_file.parent.mkdir(
            parents=True, exist_ok=True
        )
        with self.settings.wacli_execution_lock_file.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def run_json(
        self, *args: str, timeout_seconds: int = 300
    ) -> tuple[Any, WacliCommandResult]:
        # All commands share one wacli store because that directory contains the
        # WhatsApp session and is guarded by wacli's exclusive lock.
        command = [*self._base_command(), *args]
        with self._execution_lock():
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()

        payload: dict[str, Any] | None = None
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise WacliError(f"Invalid JSON from wacli: {stdout}") from exc

        result = WacliCommandResult(stdout=stdout, stderr=stderr, command=command)

        if completed.returncode != 0:
            error_message = stderr
            if payload and payload.get("error"):
                error_message = str(payload["error"])
            raise WacliError(
                error_message or f"wacli exited with code {completed.returncode}"
            )

        if payload is None:
            return None, result
        if not payload.get("success", True):
            raise WacliError(
                str(payload.get("error") or "wacli returned success=false")
            )
        return payload.get("data"), result

    def doctor(self) -> tuple[dict[str, Any], WacliCommandResult]:
        data, result = self.run_json("doctor")
        return data or {}, result

    def contacts_refresh(self) -> tuple[Any, WacliCommandResult]:
        return self.run_json("contacts", "refresh")

    def contacts_search(
        self, query: str = ".", limit: int = 5000
    ) -> tuple[list[dict[str, Any]], WacliCommandResult]:
        # The current CLI rejects empty queries, so `.` is our broad-search fallback.
        data, result = self.run_json("contacts", "search", query, "--limit", str(limit))
        return list(data or []), result

    def chats_list(
        self, limit: int = 5000, query: str | None = None
    ) -> tuple[list[dict[str, Any]], WacliCommandResult]:
        args = ["chats", "list", "--limit", str(limit)]
        if query:
            args.extend(["--query", query])
        data, result = self.run_json(*args)
        return list(data or []), result

    def groups_list(self) -> tuple[list[dict[str, Any]], WacliCommandResult]:
        data, result = self.run_json("groups", "list")
        return list(data or []), result

    def sync_once(self) -> tuple[Any, WacliCommandResult]:
        return self.run_json("sync", "--once")

    def send_text(self, to: str, message: str) -> tuple[Any, WacliCommandResult]:
        return self.run_json("send", "text", "--to", to, "--message", message)

    def send_file(
        self,
        to: str,
        file_path: str,
        caption: str = "",
        mime: str = "",
        filename: str = "",
    ) -> tuple[Any, WacliCommandResult]:
        args = ["send", "file", "--to", to, "--file", file_path]
        if caption:
            args.extend(["--caption", caption])
        if mime:
            args.extend(["--mime", mime])
        if filename:
            args.extend(["--filename", filename])
        return self.run_json(*args)
