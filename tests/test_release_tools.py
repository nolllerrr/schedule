from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts import release_tools as release


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Release test")
    for name in ("Dockerfile", "compose.yaml", "uv.lock", "requirements.lock", "build-requirements.lock", "scripts/release_tools.py", "scripts/backup_tools.py", "src/current.py", "src/deleted.py"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "Initial")
    (repo / "src/deleted.py").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "Remove module")
    return repo


def test_committed_artifact_removes_deleted_modules_and_excludes_local_secrets(repository: Path, tmp_path: Path) -> None:
    (repository / ".env").write_text("private local settings", encoding="utf-8")
    (repository / "src/uncommitted.py").write_text("uncommitted", encoding="utf-8")
    artifact = tmp_path / "artifact"
    manifest = release.package(repository, "HEAD", artifact)
    assert "src/current.py" in manifest["files"]
    assert "src/deleted.py" not in manifest["files"]
    assert "src/uncommitted.py" not in manifest["files"]
    assert ".env" not in manifest["files"]
    assert release.extract_release(artifact) == manifest
    assert not (artifact / "source/src/deleted.py").exists()
    with pytest.raises(FileExistsError):
        release.extract_release(artifact)


@pytest.mark.parametrize("name,kind", [("../escape.py", "file"), ("/absolute.py", "file"), ("C:/escape.py", "file"), ("src/link", "link"), (".env", "file"), ("data/schedule.db", "file"), ("key.pem", "file"), ("schedule.db.analytics-salt", "file"), ("nested/runtime-health.json", "file"), ("runtime-health.json.12.tmp", "file")])
def test_unsafe_or_private_archive_entries_are_rejected(tmp_path: Path, name: str, kind: str) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as target:
        member = tarfile.TarInfo(name)
        if kind == "link":
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
            target.addfile(member)
        else:
            member.size = 1
            target.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe or private"):
        release.archive_manifest(archive)


def test_archive_tamper_is_rejected_before_source_creation(repository: Path, tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    release.package(repository, "HEAD", artifact)
    with (artifact / "source.tar").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        release.extract_release(artifact)
    assert not (artifact / "source").exists()


def test_dirty_deploy_is_rejected_before_any_network(repository: Path, monkeypatch) -> None:
    (repository / "src/current.py").write_text("changed", encoding="utf-8")
    monkeypatch.setattr(release, "ssh", lambda *args: pytest.fail("Unexpected SSH"))
    with pytest.raises(ValueError, match="working tree"):
        release.deploy(repository, "HEAD", {})


def driver_fixture(tmp_path: Path, *, new_schema: int = 9) -> release.ReleaseDriver:
    root = tmp_path / "project"
    directory = root / "releases" / "test-release"
    directory.mkdir(parents=True)
    (directory / "source").mkdir()
    (directory / "source/compose.yaml").write_text("services: {}", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}", encoding="utf-8")
    previous = directory / "previous-compose.yaml"
    previous.write_text("services: {}", encoding="utf-8")
    driver = release.ReleaseDriver(root, directory, "schedule-bot")
    release.write_json(driver.state_path, {
        "revision": "f" * 40,
        "phase": "healthy",
        "old_image": "sha256:old",
        "new_image": "sha256:new",
        "previous": None,
        "previous_compose": str(previous),
        "compose": str(directory / "source/compose.yaml"),
        "backup": {"snapshot": "/app/data/before.db", "schema": 8},
        "new_schema": new_schema,
    })
    release.write_json(driver.pointer, {"release": str(directory), "compose": str(directory / "source/compose.yaml"), "image": "sha256:new"})
    return driver


@pytest.mark.parametrize("schema,expected_restore", [(8, False), (9, True)])
def test_rollback_restores_schema_only_when_needed_and_uses_exact_previous_image(tmp_path: Path, monkeypatch, schema: int, expected_restore: bool) -> None:
    driver = driver_fixture(tmp_path, new_schema=schema)
    commands, actions = [], []
    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(driver, "one_shot", lambda image, action, *args: actions.append((image, action)) or {"schema": 8})
    monkeypatch.setattr(driver, "readiness", lambda *args, **kwargs: {"ready": True})
    assert driver.rollback()["rollback"] == "completed"
    assert actions == ([("sha256:old", "restore")] if expected_restore else [])
    assert release.read_json(driver.state_path)["phase"] == "rolled_back"
    overrides = list(driver.release.glob("image-*.json"))
    assert any(release.read_json(path)["services"]["bot"]["image"] == "sha256:old" for path in overrides)
    assert commands[-1][-4:] == ["--no-build", "--no-deps", "-d", "bot"]


def test_rollback_refuses_restore_while_container_is_still_running(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="container\n" if "ps" in command else "", stderr=""))
    monkeypatch.setattr(driver, "inspect", lambda container: {"State": {"Running": True}})
    monkeypatch.setattr(driver, "one_shot", lambda *args: pytest.fail("Unexpected DB restore"))
    with pytest.raises(release.OperationError, match="while bot is running"):
        driver.rollback()


def test_readiness_requires_healthy_worker_scheduler_and_schema(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:new", "RestartCount": 0, "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    responses = iter([
        {"ready": True, "schema": 9, "checked_at": "2026-10-16T00:00:01+00:00", "components": {"polling": True}},
        {"ready": True, "schema": 9, "checked_at": "2026-10-15T23:59:59+00:00", "components": {"polling": True, "delivery_worker": True, "scheduler": True}},
        {"ready": True, "schema": 9, "checked_at": "2026-10-16T00:00:02+00:00", "components": {"polling": True, "delivery_worker": True, "scheduler": True}},
    ])
    monkeypatch.setattr(release, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=json.dumps(next(responses)), stderr=""))
    monkeypatch.setattr(release.time, "sleep", lambda seconds: None)
    assert driver.readiness(driver.root / "compose.yaml", "sha256:new")["schema"] == 9


@pytest.mark.parametrize("failure_stage", ["readiness", "stop"])
def test_release_failure_invokes_snapshot_rollback(tmp_path: Path, monkeypatch, failure_stage: str) -> None:
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(release, "extract_release", lambda path: {"revision": "f" * 40})
    monkeypatch.setattr(driver, "container", lambda file: "old")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:old"})
    def fake_run(command, **kwargs):
        if failure_stage == "stop" and "stop" in command:
            raise release.OperationError("not ready after partial stop")
        return subprocess.CompletedProcess(command, 0, stdout='[{"Id":"sha256:new"}]' if command[1:3] == ["image", "inspect"] else "", stderr="")
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(driver, "one_shot", lambda image, action, *args, **kwargs: {"snapshot": "/app/data/before.db", "schema": 8 if action == "backup" else 9})
    monkeypatch.setattr(driver, "readiness", lambda *args: (_ for _ in ()).throw(release.OperationError("not ready")))
    restored = []
    monkeypatch.setattr(driver, "rollback", lambda **kwargs: restored.append(kwargs))
    with pytest.raises(release.OperationError, match="not ready"):
        driver.deploy()
    assert restored == [{"restore_database": True}]


def test_offline_rehearsal_has_readonly_production_mount_and_no_credentials(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    commands = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0, stdout='{"schema":9}', stderr=""))
    driver.one_shot("sha256:new", "rehearse", "--snapshot", "/app/data/before.db", readonly=True)
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert "--env-file" not in command
    assert any("dst=/app/data,readonly" in value for value in command)


def test_subprocess_failure_does_not_expose_sensitive_output(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 2, stdout="BOT_TOKEN=private", stderr="host/password=private"))
    with pytest.raises(release.OperationError) as error:
        release.run(["ssh", "private-server"])
    assert "private" not in str(error.value)


def test_command_timeout_is_bounded_and_sanitized(monkeypatch) -> None:
    def expired(command, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(command, 10, output="secret", stderr="private")
    monkeypatch.setattr(subprocess, "run", expired)
    with pytest.raises(release.OperationError, match="timed out") as error:
        release.run(["docker", "exec", "private-container"], timeout=10)
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_manual_rollback_cannot_target_an_older_inactive_release(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    release.write_json(driver.pointer, {"release": "another-release"})
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: pytest.fail("Unexpected container mutation"))
    with pytest.raises(release.OperationError, match="older inactive"):
        driver.rollback()


@pytest.mark.parametrize("phase", ["prepared", "switching"])
def test_stale_failed_release_cannot_roll_back_a_newer_current_release(tmp_path: Path, monkeypatch, phase: str) -> None:
    driver = driver_fixture(tmp_path)
    state = release.read_json(driver.state_path)
    state["phase"] = phase
    release.write_json(driver.state_path, state)
    release.write_json(driver.pointer, {"release": "newer-unrelated-release"})
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: pytest.fail("Unexpected container mutation"))
    with pytest.raises(release.OperationError, match="older inactive"):
        driver.rollback()


def test_legacy_rollback_does_not_override_supported_unhealthy_heartbeat(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:old", "RestartCount": 0, "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        if "logs" in command:
            pytest.fail("Supported unhealthy health must not fall back to logs")
        return subprocess.CompletedProcess(command, 1, stdout='{"ready":false,"reason":"worker_failed"}', stderr="")
    monkeypatch.setattr(release, "run", fake_run)
    ticks = iter([0, 0.1, 2])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda seconds: None)
    with pytest.raises(release.OperationError, match="deadline"):
        driver.readiness(driver.root / "compose.yaml", "sha256:old", legacy=True, timeout=1)


def test_legacy_no_health_command_can_use_polling_start_fallback(tmp_path: Path, monkeypatch) -> None:
    driver = driver_fixture(tmp_path)
    monkeypatch.setattr(driver, "container", lambda file: "container")
    monkeypatch.setattr(driver, "inspect", lambda container: {"Image": "sha256:old", "RestartCount": 0, "State": {"Running": True, "StartedAt": "2026-10-16T00:00:00Z"}})
    def fake_run(command, **kwargs):
        if "logs" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="Run polling for bot")
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="argument command: invalid choice: 'health'")
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release.time, "sleep", lambda seconds: None)
    assert driver.readiness(driver.root / "compose.yaml", "sha256:old", legacy=True)["legacy_polling_check"] is True
