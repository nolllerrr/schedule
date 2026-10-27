import json
import subprocess

import pytest
from test_release_tools import driver_fixture

from scripts import release_tools as release

HEALTH = {"ready": True, "schema": 11, "checked_at": "2026-10-16T00:00:02+00:00",
          "components": {"polling": True, "delivery_worker": True, "scheduler": True}}


@pytest.mark.parametrize("health", [None, [], "text", 12, True,
    {**HEALTH, "checked_at": None}, {**HEALTH, "checked_at": "invalid"},
    {**HEALTH, "components": {}},
    *[pytest.param({**HEALTH, "components": value}, id=f"components-{type(value).__name__}",
                  marks=pytest.mark.xfail(strict=True, reason="readiness crashes on nonmapping components"))
      for value in (None, [], "yes")],
    pytest.param({**HEALTH, "ready": "false"}, id="string-ready",
                 marks=pytest.mark.xfail(strict=True, reason="readiness accepts truthy nonboolean flags")),
    pytest.param({**HEALTH, "components": {"polling": "false", "delivery_worker": True, "scheduler": True}},
                 id="string-component", marks=pytest.mark.xfail(strict=True, reason="readiness accepts truthy nonboolean flags")),
    pytest.param({**HEALTH, "checked_at": "2099-01-01T00:00:00+00:00"}, id="future-heartbeat",
                 marks=pytest.mark.xfail(strict=True, reason="readiness accepts heartbeat from the future")),
])
def test_readiness_requires_valid_current_typed_health(tmp_path, monkeypatch, health):
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0,
        "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps(health), stderr=""))
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda delay: None)
    with pytest.raises(release.OperationError, match="deadline"):
        driver.readiness(driver.root / "compose.yaml", "sha256:new", timeout=1)
