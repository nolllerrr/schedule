"""Local liveness heartbeat; never probes external services or opens the database."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


async def run_runtime_health(
    path: Path, schema_version: int, delivery_worker: asyncio.Task,
    scheduler: object, polling_active: Callable[[], bool], interval: float = 5,
) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        while True:
            components = {
                "polling": bool(polling_active()),
                "delivery_worker": not delivery_worker.done(),
                "scheduler": bool(getattr(scheduler, "running", False)),
            }
            payload = {
                "ready": all(components.values()), "schema": schema_version,
                "components": components, "checked_at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
            }
            def publish() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(temporary, path)
            publication = asyncio.create_task(asyncio.to_thread(publish))
            try:
                await asyncio.shield(publication)
            except asyncio.CancelledError:
                await publication
                raise
            await asyncio.sleep(interval)
    finally:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)


def read_runtime_health(path: Path, *, max_age_seconds: float = 30) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(value["checked_at"])
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        if not 0 <= age <= max_age_seconds:
            value["ready"] = False
            value["reason"] = "stale_heartbeat"
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {"ready": False, "reason": "heartbeat_unavailable"}
