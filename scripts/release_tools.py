"""Versioned releases. plan/package are local; deploy/rollback explicitly use SSH.

Run --help or read scripts/OPERATIONS.md before using on an existing server.
No offsite backup transport is implemented by this tool.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time

try:
    from .backup_tools import copy_database, fingerprint, restore_stopped_database
except ImportError:  # Standalone copy uploaded to the server / mounted in a container.
    from backup_tools import copy_database, fingerprint, restore_stopped_database


class OperationError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None, check: bool = True, timeout: float = 1800) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OperationError(f"{Path(command[0]).name} operation timed out") from None
    if check and result.returncode:
        # Commands, paths, remote addresses and Docker output can contain secrets.
        raise OperationError(f"{Path(command[0]).name} operation failed (exit {result.returncode})")
    return result


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, content: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def resolve_revision(repo: Path, revision: str) -> str:
    value = run(["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], cwd=repo).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Expected a Git commit SHA")
    return value


def safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    parts = path.parts
    if path.is_absolute() or not parts or parts[0].endswith(":") or ".." in parts or "\\" in member.name:
        return False
    if not (member.isfile() or member.isdir()) or member.size > 16 * 1024 * 1024:
        return False
    if any(p in {".git", ".venv", "data", "audits", ".ssh"} for p in parts):
        return False
    name = path.name
    return not (
        name in {".env", ".deploy.json", "runtime-health.json"}
        or name.startswith("runtime-health.json.")
        or name.endswith(".analytics-salt")
        or (name.startswith(".env.") and name != ".env.example")
        or name.endswith((".db", ".db-wal", ".db-shm", ".pem", ".key", ".xlsx"))
    )


def archive_manifest(archive_path: Path) -> dict[str, str]:
    result = {}
    total = 0
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            if not safe_member(member):
                raise ValueError("Release archive contains an unsafe or private entry")
            total += member.size
            if total > 128 * 1024 * 1024:
                raise ValueError("Release archive exceeds its size budget")
            if member.isfile():
                if member.name in result:
                    raise ValueError("Duplicate archive member")
                with archive.extractfile(member) as stream:
                    result[member.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return dict(sorted(result.items()))


def package(repo: Path, revision: str, output: Path) -> dict:
    sha = resolve_revision(repo, revision)
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "source.tar"
    run(["git", "archive", "--format=tar", "--output", str(archive.resolve()), sha], cwd=repo)
    files = archive_manifest(archive)
    required = {"Dockerfile", "compose.yaml", "uv.lock", "requirements.lock", "build-requirements.lock", "scripts/release_tools.py", "scripts/backup_tools.py"}
    if not required.issubset(files):
        raise ValueError("Commit lacks the complete release tooling or dependency locks")
    manifest = {"revision": sha, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": files}
    write_json(output / "release.json", manifest)
    with tarfile.open(archive) as source:
        for name in ("release_tools.py", "backup_tools.py"):
            (output / name).write_bytes(source.extractfile("scripts/" + name).read())
    return manifest


def extract_release(release: Path) -> dict:
    manifest = read_json(release / "release.json")
    if not re.fullmatch(r"[a-f0-9]{40}", manifest["revision"]):
        raise ValueError("Invalid release revision")
    archive_path = release / "source.tar"
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Archive checksum mismatch")
    if archive_manifest(archive_path) != manifest["files"]:
        raise ValueError("Archive manifest mismatch")
    for name in ("release_tools.py", "backup_tools.py"):
        if hashlib.sha256((release / name).read_bytes()).hexdigest() != manifest["files"]["scripts/" + name]:
            raise ValueError("Release runner differs from the archived commit")
    destination = release / "source"
    destination.mkdir(exist_ok=False)  # A release is never overlaid on an old tree.
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream, path.open("xb") as target:
                    shutil.copyfileobj(stream, target)
                path.chmod(member.mode & 0o755)
    return manifest


def load_config(path: Path) -> dict:
    config = read_json(path)
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", config.get("server", "")):
        raise ValueError("Invalid SSH server")
    config["port"] = int(config.get("port", 22))
    if not 1 <= config["port"] <= 65535:
        raise ValueError("Invalid SSH port")
    root = config.get("remoteDir", "")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", root) or ".." in PurePosixPath(root).parts or root == "/":
        raise ValueError("Invalid server project directory")
    config["keyPath"] = os.path.expandvars(os.path.expanduser(config["keyPath"]))
    if not Path(config["keyPath"]).is_file():
        raise ValueError("SSH key file is unavailable")
    config.setdefault("project", "schedule-bot")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", config["project"]):
        raise ValueError("Invalid Compose project name")
    return config


def ssh(config: dict, command: list[str]) -> subprocess.CompletedProcess:
    return run(["ssh", "-i", config["keyPath"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-p", str(config["port"]), config["server"], shlex.join(command)])


def remote_action(config: dict, action: str, release_id: str, *, restore_database: bool = False) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}-[0-9]{8}T[0-9]{12}Z", release_id):
        raise ValueError("Invalid release identifier")
    release = config["remoteDir"].rstrip("/") + "/releases/" + release_id
    command = ["python3", release + "/release_tools.py", "remote-" + action, "--root", config["remoteDir"], "--release", release, "--project", config["project"]]
    if restore_database:
        command.append("--restore-database")
    result = ssh(config, command)
    return json.loads(result.stdout)


def deploy(repo: Path, revision: str, config: dict) -> dict:
    if run(["git", "status", "--porcelain"], cwd=repo).stdout.strip():
        raise ValueError("Commit or remove working tree changes before deployment")
    with tempfile.TemporaryDirectory(prefix="schedule-release-") as temporary:
        artifact = Path(temporary) / "artifact"
        manifest = package(repo, revision, artifact)
        release_id = manifest["revision"] + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        remote = config["remoteDir"].rstrip("/") + "/releases/" + release_id
        ssh(config, ["mkdir", "-p", remote])
        run(["scp", "-i", config["keyPath"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-P", str(config["port"]), *[str(artifact / name) for name in ("source.tar", "release.json", "release_tools.py", "backup_tools.py")], config["server"] + ":" + remote + "/"])
        return remote_action(config, "deploy", release_id)


@contextmanager
def deployment_lock(root: Path):
    lock = root / ".deployment.lock"
    lock.mkdir()  # Also blocks overlapping deploy/rollback processes.
    try:
        yield
    finally:
        lock.rmdir()


class ReleaseDriver:
    def __init__(self, root: Path, release: Path, project: str):
        self.root, self.release, self.project = root.resolve(), release.resolve(), project
        if self.root == Path("/") or self.release.parent != self.root / "releases":
            raise ValueError("Release must be inside the named project's releases directory")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise ValueError("Invalid Compose project name")
        self.state_path = self.release / "state.json"
        self.pointer = self.root / ".current-release.json"

    def compose(self, file: Path, *arguments: str, image: str | None = None) -> list[str]:
        command = ["docker", "compose", "--project-name", self.project, "--project-directory", str(self.root), "-f", str(file)]
        if image is not None:
            override = self.release / ("image-" + hashlib.sha256(image.encode()).hexdigest()[:12] + ".json")
            write_json(override, {"services": {"bot": {"image": image}}})
            command += ["-f", str(override)]
        return command + list(arguments)

    def container(self, file: Path) -> str:
        ids = run(self.compose(file, "ps", "--all", "--quiet", "bot")).stdout.split()
        if len(ids) != 1:
            raise OperationError("Expected exactly one existing bot container")
        return ids[0]

    def inspect(self, container: str) -> dict:
        return json.loads(run(["docker", "inspect", container]).stdout)[0]

    def one_shot(self, image: str, action: str, *arguments: str, readonly: bool = False) -> dict:
        command = ["docker", "run", "--rm", "--network", "none"]
        if not readonly:
            command += ["--env-file", str(self.root / ".env")]
        command += ["--mount", "type=bind,src=" + str(self.root / "data") + ",dst=/app/data" + (",readonly" if readonly else ""), "--mount", "type=bind,src=" + str(self.release) + ",dst=/release-tools,readonly", "--entrypoint", "python", image, "/release-tools/release_tools.py", "database-" + action, *arguments]
        return json.loads(run(command).stdout)

    def readiness(self, file: Path, expected_image: str, *, legacy: bool = False, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        observed = 0
        while time.monotonic() < deadline:
            container = self.container(file)
            info = self.inspect(container)
            if info["Image"] != expected_image or info.get("RestartCount", 0):
                raise OperationError("Unexpected image or restarting bot container")
            if info["State"]["Running"]:
                response = run(["docker", "exec", container, "schedule-bot", "health", "--json"], check=False, timeout=10)
                unsupported_health = response.returncode == 2 and any(marker in response.stderr for marker in ("invalid choice", "unrecognized arguments"))
                if response.returncode == 0:
                    try:
                        health = json.loads(response.stdout)
                        checked = datetime.fromisoformat(health["checked_at"])
                        started = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00"))
                        belongs_to_current_start = checked.tzinfo is not None and checked >= started
                    except (ValueError, KeyError, TypeError):
                        health = {}
                        belongs_to_current_start = False
                    if belongs_to_current_start and health.get("ready") and all(health.get("components", {}).get(component) for component in ("polling", "delivery_worker", "scheduler")):
                        return health
                if legacy and unsupported_health:
                    observed += 1
                    log_result = run(["docker", "logs", "--since", info["State"]["StartedAt"], container])
                    # Docker sends Python logging to stderr, captured separately.
                    logs = log_result.stdout + log_result.stderr
                    if observed >= 3 and ("Run polling for bot" in logs or "Start polling" in logs):
                        return {"ready": True, "legacy_polling_check": True}
            time.sleep(2)
        raise OperationError("Bot readiness deadline exceeded")

    def deploy(self) -> dict:
        manifest = extract_release(self.release)
        tag = "schedule-bot-release:" + manifest["revision"]
        run(["docker", "build", "--label", "org.opencontainers.image.revision=" + manifest["revision"], "--tag", tag, str(self.release / "source")])
        new_image = json.loads(run(["docker", "image", "inspect", tag]).stdout)[0]["Id"]
        prior = read_json(self.pointer) if self.pointer.exists() else None
        current_compose = Path(prior["compose"]) if prior else self.root / "compose.yaml"
        container = self.container(current_compose)
        old = self.inspect(container)
        old_image = old["Image"]
        run(["docker", "image", "tag", old_image, "schedule-bot-rollback:" + self.release.name])
        previous_compose = self.release / "previous-compose.yaml"
        shutil.copyfile(current_compose, previous_compose)
        state = {"revision": manifest["revision"], "phase": "prepared", "old_image": old_image, "new_image": new_image, "previous": prior, "previous_compose": str(previous_compose), "compose": str(self.release / "source" / "compose.yaml")}
        write_json(self.state_path, state)
        try:
            run(self.compose(current_compose, "stop", "--timeout", "40", "bot"))
            snapshot = "/app/data/deployment-snapshots/" + self.release.name + ".db"
            state["backup"] = self.one_shot(old_image, "backup", "--destination", snapshot)
            write_json(self.state_path, state)
            rehearsal = self.one_shot(new_image, "rehearse", "--snapshot", snapshot, readonly=True)
            state["new_schema"] = rehearsal["schema"]
            state["phase"] = "switching"
            write_json(self.state_path, state)
            run(self.compose(Path(state["compose"]), "up", "--no-build", "--no-deps", "-d", "bot", image=new_image))
            health = self.readiness(Path(state["compose"]), new_image)
            if health.get("schema") != state["new_schema"]:
                raise OperationError("Running schema differs from rehearsal")
            write_json(self.pointer, {"release": str(self.release), "compose": state["compose"], "image": new_image})
            state["phase"] = "healthy"
            write_json(self.state_path, state)
            return {"deployed": state["revision"], "release": self.release.name, "ready": True, "schema": state["new_schema"]}
        except BaseException:
            self.rollback(restore_database=True)
            raise

    def rollback(self, *, restore_database: bool = False) -> dict:
        state = read_json(self.state_path)
        if state.get("phase") == "rolled_back":
            raise OperationError("Release has already been rolled back")
        current = read_json(self.pointer) if self.pointer.exists() else None
        owns_current = current is not None and current.get("release") == str(self.release)
        own_failed_switch = state["phase"] in {"prepared", "switching"} and current == state["previous"]
        if not (owns_current or own_failed_switch):
            raise OperationError("Cannot roll back an older inactive release")
        file = Path(state["compose"])
        run(self.compose(file, "stop", "--timeout", "40", "bot", image=state["new_image"]))
        if any(self.inspect(c)["State"]["Running"] for c in run(self.compose(file, "ps", "--all", "--quiet", "bot", image=state["new_image"])).stdout.split()):
            raise OperationError("Refusing database restoration while bot is running")
        incompatible = "backup" in state and state.get("new_schema") != state["backup"]["schema"]
        if "backup" in state and (restore_database or incompatible):
            snapshot = state["backup"]["snapshot"]
            restored = self.one_shot(state["old_image"], "restore", "--snapshot", snapshot, "--preserved", snapshot + ".failed-deployment")
            if restored.get("schema") != state["backup"]["schema"]:
                raise OperationError("Restored schema differs from the rollback snapshot")
        previous = Path(state["previous_compose"])
        run(self.compose(previous, "up", "--no-build", "--no-deps", "-d", "bot", image=state["old_image"]))
        self.readiness(previous, state["old_image"], legacy=state["previous"] is None)
        state["phase"] = "rolled_back"
        write_json(self.state_path, state)
        if state["previous"]:
            write_json(self.pointer, state["previous"])
        else:
            self.pointer.unlink(missing_ok=True)
        return {"rollback": "completed", "release": self.release.name}


def configured_database() -> Path:
    from schedule_bot.config import Settings
    path = Settings.from_env(require_bot_token=False).database_path.resolve()
    if not path.is_relative_to(Path("/app/data")):
        raise ValueError("Deployment requires DATABASE_PATH inside /app/data")
    return path


def database_action(args) -> dict:
    if args.command == "database-backup":
        path = configured_database()
        result = copy_database(path, args.destination)
        return {"snapshot": str(args.destination), "schema": result["schema"]}
    if args.command == "database-restore":
        result = restore_stopped_database(args.snapshot, configured_database(), args.preserved)
        return {"restored": True, "schema": result["schema"]}
    from schedule_bot.repository import ScheduleRepository
    before = fingerprint(args.snapshot)
    with tempfile.TemporaryDirectory(prefix="schedule-rehearsal-") as temporary:
        target = Path(temporary) / "schedule.db"
        copy_database(args.snapshot, target)
        repository = ScheduleRepository(target)
        repository.initialize()
        after = fingerprint(target)
        for name, original in before["tables"].items():
            if name not in after["tables"] or after["tables"][name]["count"] != original["count"]:
                raise OperationError("Migration rehearsal lost existing database records")
        return {"schema": after["schema"], "preserved_tables": len(before["tables"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "package", "deploy"):
        command = commands.add_parser(name)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--revision", default="HEAD")
        if name == "package":
            command.add_argument("--output", type=Path, required=True)
        if name == "deploy":
            command.add_argument("--config", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--config", type=Path, required=True)
    rollback.add_argument("--release", required=True)
    rollback.add_argument("--restore-database", action="store_true", help="Discard changes after the release snapshot even for a compatible schema")
    for name in ("remote-deploy", "remote-rollback"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--release", type=Path, required=True)
        command.add_argument("--project", required=True)
        if name == "remote-rollback":
            command.add_argument("--restore-database", action="store_true")
    backup = commands.add_parser("database-backup")
    backup.add_argument("--destination", type=Path, required=True)
    rehearsal = commands.add_parser("database-rehearse")
    rehearsal.add_argument("--snapshot", type=Path, required=True)
    restore = commands.add_parser("database-restore")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--preserved", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = {"revision": resolve_revision(args.repo, args.revision), "network": False, "stages": ["clean committed archive", "verify manifest", "build image", "stop existing bot", "verified backup", "offline migration rehearsal", "start exact image", "heartbeat readiness", "restore old image and database on failure"]}
        elif args.command == "package":
            result = package(args.repo, args.revision, args.output)
        elif args.command == "deploy":
            result = deploy(args.repo, args.revision, load_config(args.config))
        elif args.command == "rollback":
            result = remote_action(load_config(args.config), "rollback", args.release, restore_database=args.restore_database)
        elif args.command.startswith("remote-"):
            driver = ReleaseDriver(args.root, args.release, args.project)
            with deployment_lock(driver.root):
                result = driver.deploy() if args.command == "remote-deploy" else driver.rollback(restore_database=args.restore_database)
        else:
            result = database_action(args)
        print(json.dumps(result))
    except (Exception, KeyboardInterrupt) as error:
        # Never dump subprocess output, environment, config or raw exceptions.
        result = {"ok": False, "error": type(error).__name__}
        if isinstance(error, OperationError):
            result["operation"] = str(error)
        print(json.dumps(result))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
