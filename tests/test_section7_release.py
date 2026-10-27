import json
import sqlite3
import subprocess
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_backup_tools import make_database

from scripts import backup_tools
from scripts import release_tools as release


@pytest.mark.parametrize("schema_mismatch", [False, True])
def test_release_uses_snapshot_rehearsal_and_commits_pointer_only_after_readiness(tmp_path, monkeypatch, schema_mismatch):
    root = tmp_path / "project"
    directory = root / "releases" / "candidate"
    directory.mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}", encoding="utf-8")
    database = root / "working.db"
    snapshot = root / "snapshot.db"
    preserved = root / "failed.db"
    make_database(database, "sentinel", version=0)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id), value TEXT)")
        connection.execute("INSERT INTO parent VALUES (1)")
        connection.execute("INSERT INTO child VALUES (1, 'preserve child')")
    before = backup_tools.fingerprint(database)
    previous = {"release": "previous", "compose": str(root / "compose.yaml"), "image": "sha256:old"}
    release.write_json(root / ".current-release.json", previous)
    driver = release.ReleaseDriver(root, directory, "schedule-bot")
    events = []
    rehearsal_fingerprints = []

    def extract(path):
        source = path / "source"
        source.mkdir()
        (source / "compose.yaml").write_text("services: {}", encoding="utf-8")
        return {"revision": "a" * 40}

    def run(command, **kwargs):
        events.append(("command", command))
        output = json.dumps([{"Id": "sha256:new"}]) if command[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def one_shot(image, action, *args, readonly=False):
        events.append((action, image, readonly))
        monkeypatch.setattr(release, "configured_database", lambda: database)
        if action == "backup":
            return release.database_action(SimpleNamespace(command="database-backup", destination=snapshot))
        if action == "rehearse":
            return release.database_action(SimpleNamespace(command="database-rehearse", snapshot=snapshot))
        assert action == "restore"
        return release.database_action(SimpleNamespace(command="database-restore", snapshot=snapshot, preserved=preserved))

    def readiness(file, image, **kwargs):
        events.append(("readiness", image))
        if image == "sha256:new":
            assert release.read_json(driver.pointer) == previous
            return {"ready": True, "schema": 0 if schema_mismatch else 11}
        assert image == "sha256:old"
        return {"ready": True, "schema": 0}

    monkeypatch.setattr(release, "extract_release", extract)
    monkeypatch.setattr(release, "run", run)
    def fingerprint(path):
        result = backup_tools.fingerprint(path)
        rehearsal_fingerprints.append(result)
        return result
    monkeypatch.setattr(release, "fingerprint", fingerprint)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:old", "State": {"Running": False}})
    monkeypatch.setattr(driver, "one_shot", one_shot)
    monkeypatch.setattr(driver, "readiness", readiness)
    if schema_mismatch:
        with pytest.raises(release.OperationError, match="schema differs"):
            driver.deploy()
        assert release.read_json(driver.pointer) == previous
        assert release.read_json(driver.state_path)["phase"] == "rolled_back"
        assert backup_tools.fingerprint(database) == before
        assert backup_tools.fingerprint(preserved) == before
        assert ("restore", "sha256:old", False) in events
    else:
        result = driver.deploy()
        assert result["ready"] and result["schema"] == 11
        assert release.read_json(driver.pointer)["image"] == "sha256:new"
        assert release.read_json(driver.state_path)["phase"] == "healthy"
    assert backup_tools.fingerprint(snapshot) == before
    assert len(rehearsal_fingerprints) == 2
    for table in ("records", "parent", "child"):
        assert rehearsal_fingerprints[0]["tables"][table] == rehearsal_fingerprints[1]["tables"][table]
    assert events.index(("backup", "sha256:old", False)) < events.index(("rehearse", "sha256:new", True))
    assert events.index(("rehearse", "sha256:new", True)) < events.index(("readiness", "sha256:new"))


@pytest.mark.parametrize("response", ["not json", "{}", json.dumps({"ready": True, "checked_at": "2026-10-15T00:00:00+00:00", "components": {"polling": True, "scheduler": True, "delivery_worker": True}}), json.dumps({"ready": True, "checked_at": "2026-10-16T00:00:02", "components": {"polling": True, "scheduler": True, "delivery_worker": True}})])
def test_readiness_rejects_malformed_or_stale_health(tmp_path, monkeypatch, response):
    from test_release_tools import driver_fixture
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0, "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=response, stderr=""))
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda seconds: None)
    with pytest.raises(release.OperationError, match="deadline"):
        driver.readiness(driver.root / "compose.yaml", "sha256:new", timeout=1)


@pytest.mark.parametrize("image,restarts", [("sha256:wrong", 0), ("sha256:new", 1)])
def test_readiness_rejects_wrong_image_or_restart(tmp_path, monkeypatch, image, restarts):
    from test_release_tools import driver_fixture
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": image, "RestartCount": restarts})
    with pytest.raises(release.OperationError, match="image or restarting"):
        driver.readiness(driver.root / "compose.yaml", "sha256:new")
