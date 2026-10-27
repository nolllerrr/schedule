import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from test_release_tools import driver_fixture

from scripts import release_tools as release

HEALTH = {"ready": True, "schema": 11, "checked_at": "2026-10-16T00:00:02+00:00",
          "components": {"polling": True, "delivery_worker": True, "scheduler": True}}


NOW = datetime(2026, 10, 16, 0, 0, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)
    monkeypatch.setattr(release, "datetime", FixedDatetime)


@pytest.mark.parametrize("health", [None, [], "text", 12, True,
    {**HEALTH, "checked_at": None}, {**HEALTH, "checked_at": "invalid"},
    {**HEALTH, "components": {}},
    *[pytest.param({**HEALTH, "components": value}, id=f"components-{type(value).__name__}")
      for value in (None, [], "yes")],
    pytest.param({**HEALTH, "ready": "false"}, id="string-ready"),
    pytest.param({**HEALTH, "components": {"polling": "false", "delivery_worker": True, "scheduler": True}}, id="string-component"),
    pytest.param({**HEALTH, "checked_at": "2099-01-01T00:00:00+00:00"}, id="future-heartbeat"),
    {key: value for key, value in HEALTH.items() if key != "components"},
    {key: value for key, value in HEALTH.items() if key != "ready"},
    {key: value for key, value in HEALTH.items() if key != "checked_at"},
    {**HEALTH, "ready": 1},
    {**HEALTH, "checked_at": "2026-10-16T00:00:02"},
    {**HEALTH, "checked_at": "2026-10-15T23:59:31+00:00"},
    {**HEALTH, "checked_at": "2026-10-16T00:00:02.000001+00:00"},
    *[{**HEALTH, "components": {**HEALTH["components"], key: value}}
      for key in HEALTH["components"] for value in ("false", 1, False)],
    *[{**HEALTH, "components": {k: v for k, v in HEALTH["components"].items() if k != key}}
      for key in HEALTH["components"]],
])
def test_readiness_requires_valid_current_typed_health(tmp_path, monkeypatch, health):
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0,
        "State": {"Running": True, "StartedAt": "2026-10-15T23:59:00Z"}})
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps(health), stderr=""))
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda delay: None)
    with pytest.raises(release.OperationError, match="deadline"):
        driver.readiness(driver.root / "compose.yaml", "sha256:new", timeout=1)


@pytest.mark.parametrize("age", [0, 5, 30])
def test_readiness_accepts_fresh_heartbeat_boundaries(tmp_path, monkeypatch, age):
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0,
        "State": {"Running": True, "StartedAt": "2026-10-15T23:59:00Z"}})
    health = {**HEALTH, "checked_at": (NOW - timedelta(seconds=age)).isoformat()}
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps(health), stderr=""))
    assert driver.readiness(driver.root / "compose.yaml", "sha256:new") == health


def test_readiness_waits_for_heartbeat_from_current_container_start(tmp_path, monkeypatch):
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0,
        "State": {"Running": True, "StartedAt": "2026-10-16T00:00:01Z"}})
    responses = iter([{**HEALTH, "checked_at": "2026-10-16T00:00:00+00:00"}, HEALTH])
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps(next(responses)), stderr=""))
    monkeypatch.setattr(release.time, "sleep", lambda delay: None)
    assert driver.readiness(driver.root / "compose.yaml", "sha256:new") == HEALTH


def test_invalid_heartbeat_triggers_real_rollback(tmp_path, monkeypatch):
    driver = driver_fixture(tmp_path)
    previous = {"release": "previous", "compose": str(driver.root / "compose.yaml"), "image": "sha256:old"}
    release.write_json(driver.pointer, previous)
    monkeypatch.setattr(release, "extract_release", lambda path: {"revision": "f" * 40})
    active = {"image": "sha256:old"}
    events = []
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": active["image"], "RestartCount": 0,
        "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout='[{"Id":"sha256:new"}]', stderr="")
        if "up" in command:
            override = next(part for part in command if part.endswith(".json"))
            active["image"] = release.read_json(__import__("pathlib").Path(override))["services"]["bot"]["image"]
            events.append(active["image"])
        if "exec" in command:
            health = {**HEALTH, "ready": "false"} if active["image"] == "sha256:new" else HEALTH
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(health), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    def one_shot(image, action, *args, **kwargs):
        events.append(action)
        return {"snapshot": "snapshot.db", "schema": 11}
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(driver, "one_shot", one_shot)
    ticks = iter((0, 0, 121, 122, 122))
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda delay: None)
    with pytest.raises(release.OperationError, match="deadline"):
        driver.deploy()
    assert release.read_json(driver.pointer) == previous
    assert release.read_json(driver.state_path)["phase"] == "rolled_back"
    assert events == ["backup", "rehearse", "sha256:new", "restore", "sha256:old"]
