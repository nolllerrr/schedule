# Release tools

`release_tools.py plan` and `package` are local operations. `deploy` and
`rollback` explicitly connect to the configured server. No external backup
storage or transport is configured by these tools.

## Dependencies and CI

The production image uses the Python image digest in `Dockerfile`, hashed
`requirements.lock` and `build-requirements.lock`. `uv.lock` is the source for
the runtime/development exports; `scripts/build-requirements.in` pins the build
backend and wheel tooling. To update them intentionally:

```sh
uv lock --upgrade
python scripts/update_locks.py
python -m pytest
```

CI checks export freshness, Ruff F, pytest, image construction and an offline
SQLite startup. `checks / test-and-build` must additionally be configured as a
required check in the repository rules; the workflow does not change GitHub
protection settings itself.

## Deployment

The existing `.deploy.json` format is supported: `server`, `port`, `remoteDir`,
`keyPath`; an optional `project` selects the existing Compose project name
(default `schedule-bot`). Keep this file and SSH keys out of Git. The server
must already have Python 3.12+, Docker/Compose, its own `.env`, a writable
`data/` and exactly one existing bot container. `DATABASE_PATH` must resolve
inside `/app/data`; all its database users must be this single bot instance.
No other process may write this database during deploy/rollback.

```powershell
./deploy.ps1 -Plan
./deploy.ps1 -PackageDirectory ../release-artifact
./deploy.ps1
```

Deployment requires a clean committed working tree. It packages the selected
commit, rejects private files and symlinks, verifies a hash manifest, builds
from a NEW release directory, and labels/tags the image with the full commit.
Deleting a source module therefore also removes it from the next image.

After building, the driver saves the previous image and Compose file, stops
the bot, makes a verified SQLite snapshot, and rehearses initialization on a
copy in a container with no network, a read-only data mount and no production
environment file. It then starts the exact image ID and requires a fresh
`schedule-bot health --json` response for polling, worker and scheduler, with
the expected schema and no container restarts. Failure restores the old image
and snapshot. Snapshots are kept under `data/deployment-snapshots`; release
manifests/state are under `releases/<SHA>-<UTC timestamp>/`.

The current release pointer is `.current-release.json`. Deployments use the
same Compose project/service as the original installation; the old root
`src/` directory is never used as the new build context. To inspect the running
instance, use the Compose path recorded in this pointer, or `docker ps`.
Do not run the old root `docker compose up --build` after a versioned release:
that would switch back to its old source tree.

The deployment lock `.deployment.lock` blocks simultaneous operations. If a
machine/process dies, first inspect containers, state and the saved database;
remove an abandoned lock only after confirming no deployment is running.

## Rollback and restore drills

```powershell
./deploy.ps1 -RollbackRelease '<SHA>-<UTC timestamp>'
```

An incompatible schema restores the pre-release snapshot together with the
previous image. For the same schema, manual rollback keeps current data;
`-RestoreDatabase` explicitly restores its snapshot too. Automatic rollback
after a failed switch restores the snapshot. A snapshot rollback discards
changes since its creation and can repeat external notifications whose
`sent` state was recorded after that time. Use an isolated database and fake
Telegram API for rehearsal. Never run a second polling bot on a real token.

The failed database is preserved separately before an in-place restore;
its WAL/SHM are saved under `.original-wal`/`.original-shm` names and are not
attached to the restored database. Old images without the heartbeat command
are checked for a stable container and a polling-start log on rollback.

Offline restore drills always create a NEW destination and refuse overwrite:

```sh
python scripts/backup_tools.py verify path/to/backup.db
python scripts/backup_tools.py restore path/to/backup.db isolated/restored.db
```

Both operations verify SQLite integrity and foreign keys. Reports include only
schema, row counts, column names and hashes; they do not include row contents.
The public restore command never replaces a working database. Local copies do
not protect against losing the whole server; external backups remain outside
the scope of this implementation.
