import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from test_backup_tools import make_database

from scripts import backup_tools
from scripts import release_tools as release


@pytest.mark.parametrize("existing", ["preserved", "candidate"])
def test_restore_refuses_existing_artifact_without_overwrite(tmp_path, existing):
    source, current, preserved = (tmp_path / name for name in ("source.db", "current.db", "preserved.db"))
    make_database(source, "old", 7)
    make_database(current, "new", 9)
    before = backup_tools.fingerprint(current)
    conflict = preserved if existing == "preserved" else current.with_name(current.name + ".restore-candidate")
    conflict.write_bytes(b"already exists")
    with pytest.raises(ValueError):
        backup_tools.restore_stopped_database(source, current, preserved)
    assert conflict.read_bytes() == b"already exists"
    assert backup_tools.fingerprint(current) == before


def test_failed_replace_preserves_current_database_and_removes_candidate(tmp_path, monkeypatch):
    source, current, preserved = (tmp_path / name for name in ("source.db", "current.db", "preserved.db"))
    make_database(source, "old", 7)
    make_database(current, "new", 9)
    before = backup_tools.fingerprint(current)
    def fail_replace(source_path, target_path):
        raise OSError("synthetic replace error")
    monkeypatch.setattr(backup_tools.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace error"):
        backup_tools.restore_stopped_database(source, current, preserved)
    assert backup_tools.fingerprint(current) == before
    assert backup_tools.fingerprint(preserved) == before
    assert not current.with_name(current.name + ".restore-candidate").exists()


def test_restore_preserves_wal_and_shm_and_failed_database(tmp_path):
    source, live, current, preserved = (tmp_path / name for name in ("source.db", "live.db", "current.db", "preserved.db"))
    make_database(source, "old", 7)
    with closing(sqlite3.connect(live)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records VALUES (1, 'uncheckpointed')")
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(live) + suffix, str(current) + suffix)
        sidecars = {suffix: (tmp_path / ("current.db" + suffix)).read_bytes() for suffix in ("-wal", "-shm")}
        backup_tools.restore_stopped_database(source, current, preserved)
    assert backup_tools.fingerprint(current) == backup_tools.fingerprint(source)
    with closing(sqlite3.connect(preserved)) as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [("uncheckpointed",)]
    assert (tmp_path / "preserved.db.original-wal").read_bytes() == sidecars["-wal"]
    assert (tmp_path / "preserved.db.original-shm").stat().st_size == len(sidecars["-shm"])


@pytest.mark.xfail(strict=True, reason="failed restore replacement leaves original WAL at preservation path")
def test_failed_replace_keeps_original_wal_data_readable(tmp_path, monkeypatch):
    source, live, current, preserved = (tmp_path / name for name in ("source.db", "live.db", "current.db", "preserved.db"))
    make_database(source, "old", 7)
    with closing(sqlite3.connect(live)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records VALUES (1, 'uncheckpointed')")
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(live) + suffix, str(current) + suffix)
        def fail_replace(source_path, target_path):
            raise OSError("synthetic replace error")
        monkeypatch.setattr(backup_tools.os, "replace", fail_replace)
        with pytest.raises(OSError, match="synthetic replace error"):
            backup_tools.restore_stopped_database(source, current, preserved)
        with closing(sqlite3.connect(preserved)) as saved:
            assert saved.execute("SELECT value FROM records").fetchall() == [("uncheckpointed",)]
        assert (tmp_path / "preserved.db.original-wal").exists()
        with closing(sqlite3.connect(current)) as original:
            assert original.execute("SELECT value FROM records").fetchall() == [("uncheckpointed",)]


def test_deployment_lock_blocks_nested_run_and_cleans_on_error(tmp_path):
    with pytest.raises(RuntimeError, match="synthetic"), release.deployment_lock(tmp_path):
        with pytest.raises(FileExistsError), release.deployment_lock(tmp_path):
            pass
        raise RuntimeError("synthetic")
    assert not (tmp_path / ".deployment.lock").exists()


@pytest.mark.parametrize("field,value", [("server", "bad host"), ("port", 0),
                                          ("remoteDir", "/../data"), ("project", "BAD")])
def test_invalid_deployment_config_rejected_before_external_calls(tmp_path, monkeypatch, field, value):
    key = tmp_path / "test.key"
    key.touch()
    config = {"server": "user@example.test", "port": 22, "remoteDir": "/srv/schedule", "project": "schedule-bot", "keyPath": str(key)}
    config[field] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: pytest.fail("external process invoked"))
    with pytest.raises(ValueError):
        release.load_config(path)


def test_ssh_arguments_are_quoted_and_never_run_network(tmp_path, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
    monkeypatch.setattr(release, "run", fake_run)
    config = {"keyPath": str(tmp_path / "key with space"), "server": "user@example.test", "port": 2222}
    release.ssh(config, ["python3", "/srv/test path/tool.py", "--project", "schedule-bot"])
    assert calls == [["ssh", "-i", config["keyPath"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-p", "2222", config["server"], shlex.join(["python3", "/srv/test path/tool.py", "--project", "schedule-bot"])]]


def test_deploy_builds_scp_arguments_without_external_processes(tmp_path, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    def package(repo, revision, output):
        output.mkdir()
        for name in ("source.tar", "release.json", "release_tools.py", "backup_tools.py"):
            (output / name).touch()
        return {"revision": "a" * 40}
    remote_actions = []
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release, "package", package)
    monkeypatch.setattr(release, "remote_action", lambda config, action, release_id: remote_actions.append((action, release_id)) or {"ready": True})
    config = {"keyPath": str(tmp_path / "key with space"), "server": "user@example.test", "port": 2222, "remoteDir": "/srv/schedule", "project": "schedule-bot"}
    assert release.deploy(tmp_path, "HEAD", config) == {"ready": True}
    ssh_command, scp_command = calls[1:]
    assert ssh_command[:10] == ["ssh", "-i", config["keyPath"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-p", "2222", config["server"]]
    assert scp_command[:9] == ["scp", "-i", config["keyPath"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-P", "2222"]
    assert [__import__("pathlib").Path(item).name for item in scp_command[9:13]] == ["source.tar", "release.json", "release_tools.py", "backup_tools.py"]
    assert scp_command[-1].startswith("user@example.test:/srv/schedule/releases/")
    assert remote_actions[0][0] == "deploy"


def test_database_actions_use_local_cli_and_return_safe_json(tmp_path, monkeypatch, capsys):
    source, snapshot = tmp_path / "source.db", tmp_path / "snapshot.db"
    make_database(source, "sentinel", version=7)
    monkeypatch.setattr(release, "configured_database", lambda: source)
    monkeypatch.setattr(sys, "argv", ["release_tools", "database-backup", "--destination", str(snapshot)])
    release.main()
    result = json.loads(capsys.readouterr().out)
    assert result == {"snapshot": str(snapshot), "schema": 7}
    assert backup_tools.fingerprint(snapshot) == backup_tools.fingerprint(source)
    monkeypatch.setattr(sys, "argv", ["release_tools", "database-restore", "--snapshot", str(tmp_path / "missing"), "--preserved", str(tmp_path / "saved")])
    with pytest.raises(SystemExit) as caught:
        release.main()
    assert caught.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": False, "error": "ValueError"}
