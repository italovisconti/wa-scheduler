from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    database_url: str
    wacli_bin: str
    wacli_store_dir: Path
    wacli_execution_lock_file: Path
    default_timezone: str
    worker_poll_seconds: int
    materialize_horizon_minutes: int
    max_send_attempts: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    project_root = Path(__file__).resolve().parents[1]
    _load_dotenv(project_root / ".env")

    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)

    database_url = os.getenv(
        "WA_SCHEDULER_DATABASE_URL",
        f"sqlite:///{(data_dir / 'wa-scheduler.db').resolve()}",
    )
    wacli_store_dir = Path(
        os.getenv("WACLI_STORE_DIR", str(Path.home() / ".wacli"))
    ).expanduser()

    return Settings(
        project_root=project_root,
        data_dir=data_dir,
        database_url=database_url,
        wacli_bin=os.getenv("WACLI_BIN", "wacli"),
        wacli_store_dir=wacli_store_dir,
        wacli_execution_lock_file=data_dir / "wacli-execution.lock",
        default_timezone=os.getenv("WA_SCHEDULER_DEFAULT_TIMEZONE", "UTC"),
        worker_poll_seconds=_env_int("WA_SCHEDULER_WORKER_POLL_SECONDS", 30),
        materialize_horizon_minutes=_env_int(
            "WA_SCHEDULER_MATERIALIZE_HORIZON_MINUTES", 24 * 60
        ),
        max_send_attempts=_env_int("WA_SCHEDULER_MAX_SEND_ATTEMPTS", 3),
    )
