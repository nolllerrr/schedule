import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_tools as release


@pytest.mark.parametrize("stage", ["extract", "build", "image", "preflight", "tag", "prepared", "stop", "backup", "rehearse", "start", "readiness", "pointer", "healthy"])
def test_release_failure_at_each_boundary_preserves_previous_pointer(tmp_path, monkeypatch, stage):
    root = tmp_path / "root"
    directory = root / "releases" / "candidate"
    directory.mkdir(parents=True)
    compose = root / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    driver = release.ReleaseDriver(root, directory, "schedule-bot")
    previous = {"release": "previous", "image": "sha256:old", "compose": str(compose)}
    release.write_json(driver.pointer, previous)
    error = release.OperationError(f"injected {stage}")
    events = []
    injected = False
    def checkpoint(name):
        nonlocal injected
        events.append(name)
        if name == stage and not injected:
            injected = True
            raise error
    def extract(path):
        checkpoint("extract")
        source = path / "source"
        source.mkdir()
        (source / "compose.yaml").write_text("services: {}", encoding="utf-8")
        return {"revision": "a" * 40}
    def run(command, **kwargs):
        if command[:2] == ["docker", "build"]:
            checkpoint("build")
        elif command[:3] == ["docker", "image", "inspect"]:
            checkpoint("image")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([{"Id": "sha256:new"}]))
        elif command[:3] == ["docker", "image", "tag"]:
            checkpoint("tag")
        elif "--entrypoint" in command:
            checkpoint("preflight")
        elif "compose" in command and "stop" in command:
            checkpoint("stop")
        elif "compose" in command and "up" in command:
            override = next((part for part in command if part.endswith(".json")), None)
            image = release.read_json(__import__("pathlib").Path(override))["services"]["bot"]["image"]
            events.append(image)
            checkpoint("start" if image == "sha256:new" else "restore_old_image")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    def one_shot(image, action, *args, **kwargs):
        checkpoint(action)
        if action == "backup":
            assert image == "sha256:old"
            return {"schema": 7, "snapshot": "snapshot.db"}
        if action == "rehearse":
            assert image == "sha256:new" and kwargs["readonly"] is True
            return {"schema": 11}
        assert action == "restore" and image == "sha256:old"
        return {"schema": 7}
    def readiness(file, image, **kwargs):
        if image == "sha256:new":
            assert release.read_json(driver.pointer) == previous
            checkpoint("readiness")
        else:
            assert image == "sha256:old"
            events.append("old_ready")
        return {"ready": True, "schema": 11 if image == "sha256:new" else 7}
    write_json = release.write_json
    def write(path, value):
        if path == driver.pointer and value != previous:
            checkpoint("pointer")
        elif path == driver.state_path and value["phase"] in {"prepared", "healthy"}:
            checkpoint(value["phase"])
        write_json(path, value)
    monkeypatch.setattr(release, "extract_release", extract)
    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release, "write_json", write)
    monkeypatch.setattr(driver, "container", lambda file: "old")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:old", "State": {"Running": False}})
    monkeypatch.setattr(driver, "one_shot", one_shot)
    monkeypatch.setattr(driver, "readiness", readiness)
    with pytest.raises(release.OperationError) as caught:
        driver.deploy()
    assert caught.value is error
    assert injected
    assert release.read_json(driver.pointer) == previous
    if stage in {"stop", "backup", "rehearse", "start", "readiness", "pointer", "healthy"}:
        assert release.read_json(driver.state_path)["phase"] == "rolled_back"
        assert "old_ready" in events and "sha256:old" in events
        if stage not in {"stop", "backup"}:
            assert "restore" in events
    else:
        assert "restore_old_image" not in events


@pytest.mark.parametrize("command,extra,operation", [
    ("plan", [], "resolve_revision"), ("package", ["--output", "out"], "package"),
    ("deploy", ["--config", "config.json"], "deploy"),
    ("rollback", ["--config", "config.json", "--release", "release", "--restore-database"], "remote_action"),
    ("remote-deploy", ["--root", "root", "--release", "candidate", "--project", "schedule-bot"], "driver_deploy"),
    ("remote-rollback", ["--root", "root", "--release", "candidate", "--project", "schedule-bot", "--restore-database"], "driver_rollback"),
    ("database-backup", ["--destination", "snapshot"], "database_action"),
    ("database-restore", ["--snapshot", "snapshot", "--preserved", "saved"], "database_action"),
    ("database-rehearse", ["--snapshot", "snapshot"], "database_action"),
])
def test_cli_dispatch_is_offline_and_preserves_arguments(tmp_path, monkeypatch, capsys, command, extra, operation):
    import sys
    from contextlib import contextmanager
    from unittest.mock import Mock
    calls = {name: Mock(return_value={"ok": True}) for name in ("package", "deploy", "remote_action", "database_action", "driver_deploy", "driver_rollback")}
    calls["resolve_revision"] = Mock(return_value="a" * 40)
    for name in ("package", "deploy", "remote_action", "database_action", "resolve_revision"):
        monkeypatch.setattr(release, name, calls[name])
    monkeypatch.setattr(release, "load_config", lambda path: {"synthetic": True})
    @contextmanager
    def lock(root):
        yield
    monkeypatch.setattr(release, "deployment_lock", lock)
    monkeypatch.setattr(release, "ReleaseDriver", lambda *args: SimpleNamespace(root=args[0], deploy=calls["driver_deploy"], rollback=calls["driver_rollback"]))
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: pytest.fail("external process"))
    monkeypatch.setattr(sys, "argv", ["release_tools", command, *extra])
    release.main()
    assert json.loads(capsys.readouterr().out)
    calls[operation].assert_called_once()
    assert sum(call.call_count for call in calls.values()) == 1
    if command in {"rollback", "remote-rollback"}:
        assert calls[operation].call_args.kwargs["restore_database"] is True
    if command.startswith("database-"):
        args = calls[operation].call_args.args[0]
        assert args.command == command
        assert getattr(args, "snapshot", getattr(args, "destination", None)) == Path("snapshot")
    if command == "package":
        assert calls[operation].call_args.args[1:] == ("HEAD", Path("out"))
    if command == "rollback":
        assert calls[operation].call_args.args == ({"synthetic": True}, "rollback", "release")
