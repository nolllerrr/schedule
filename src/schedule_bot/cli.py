from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from schedule_bot.config import Settings
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.logging_setup import configure_console_logging, configure_file_logging
from schedule_bot.maintenance import backup_before_migration, create_database_backup, cleanup_technical_data
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater
from schedule_bot.runtime_health import read_runtime_health
from schedule_bot.delivery_audit import review_attempt, unresolved_attempts
from schedule_bot.parse_budget import parse_with_budget


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PTGH schedule bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_command = subparsers.add_parser("parse", help="Parse a local workbook")
    parse_command.add_argument("file", type=Path)
    parse_command.add_argument("--json", action="store_true", dest="as_json")

    import_command = subparsers.add_parser(
        "import-file", help="Import a local workbook into the database"
    )
    import_command.add_argument("file", type=Path)
    import_command.add_argument(
        "--approval-reason",
        help="Import reviewed coverage/count anomalies with a recorded reason; layout errors remain blocked",
    )
    retry_command = subparsers.add_parser(
        "retry-notifications", help="Retry reviewed unsent chunks of one delivery series"
    )
    retry_command.add_argument("import_id", type=int)
    retry_command.add_argument("destination_id", type=int)
    retry_command.add_argument("--kind", choices=("user", "chat"), required=True)

    update_command = subparsers.add_parser("update", help="Check and reconcile schedule publications")
    update_command.add_argument("--approval-reason", help="Accept reviewed coverage/count anomalies, never structural/priority conflicts")
    health_command = subparsers.add_parser("health", help="Read local runtime heartbeat without external requests")
    health_command.add_argument("--json", action="store_true", dest="as_json")
    cleanup_command = subparsers.add_parser("cleanup", help="Preview or prune old technical data")
    cleanup_command.add_argument("--apply", action="store_true")
    cleanup_command.add_argument("--limit", type=int, default=100)
    attempts_command = subparsers.add_parser("delivery-attempts", help="List unresolved external send attempts")
    attempts_command.add_argument("--limit", type=int, default=100)
    review_command = subparsers.add_parser("review-delivery-attempt", help="Record review without changing a delivery or its outcome")
    review_command.add_argument("attempt_id", type=int)
    review_command.add_argument("--reason", required=True)
    subparsers.add_parser("doctor", help="Check the website and Telegram API")
    subparsers.add_parser("backup", help="Create a verified database backup")
    subparsers.add_parser("run", help="Run the Telegram bot")
    return parser


def _prepare_repository(settings: Settings) -> ScheduleRepository:
    repository = ScheduleRepository(settings.database_path)
    backup_before_migration(repository, settings.backups_path)
    repository.initialize()
    return repository


async def _update(settings: Settings, *, approval_reason: str | None = None) -> None:
    repository = _prepare_repository(settings)
    updater = ScheduleUpdater(
        ScheduleDownloader(
            settings.schedule_page_url,
            settings.downloads_path,
            building=1,
            proxy_url=settings.schedule_proxy_url,
            max_attempts=settings.schedule_http_max_attempts,
            retry_max_delay=settings.schedule_http_retry_max_delay,
            lookback_days=settings.publication_lookback_days,
            lookahead_days=settings.publication_lookahead_days,
            max_publications=settings.publication_max_files,
        ),
        ExcelScheduleParser(),
        repository,
    )
    result = await updater.update_once(approval_reason=approval_reason)
    status = "already imported" if result.skipped else "imported"
    print(
        f"{result.filename}: {status}; "
        f"lessons={result.lesson_count}; changes={len(result.changes)}"
    )


async def _doctor(settings: Settings) -> None:
    from aiogram import Bot

    from schedule_bot.bot.session import create_telegram_session

    downloader = ScheduleDownloader(
        settings.schedule_page_url,
        settings.downloads_path,
        building=1,
        proxy_url=settings.schedule_proxy_url,
        max_attempts=settings.schedule_http_max_attempts,
        retry_max_delay=settings.schedule_http_retry_max_delay,
    )
    link = await downloader.find_latest()
    print(f"schedule website: OK ({link.filename})")

    session = create_telegram_session(
        proxy_url=settings.telegram_proxy_url,
        force_ipv4=settings.telegram_force_ipv4,
        request_retries=settings.telegram_request_retries,
    )
    bot = Bot(settings.bot_token, session=session)
    try:
        user = await bot.get_me()
        print(f"telegram api: OK (@{user.username}, id={user.id})")
    finally:
        await bot.session.close()


def main() -> None:
    configure_console_logging()
    arguments = build_parser().parse_args()

    if arguments.command == "parse":
        parsed = asyncio.run(parse_with_budget(arguments.file))
        if arguments.as_json:
            print(
                json.dumps(
                    [lesson.to_dict() for lesson in parsed.lessons],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                f"{parsed.source_filename}: {len(parsed.lessons)} lessons, "
                f"{len(parsed.groups)} groups, "
                f"{parsed.start_date}..{parsed.end_date}"
            )
            for warning in parsed.warnings:
                print(f"warning: {warning}")
        return

    settings = Settings.from_env(
        require_bot_token=arguments.command in {"run", "doctor"}
    )
    if arguments.command == "health":
        health = read_runtime_health(settings.database_path.parent / "runtime-health.json")
        print(json.dumps(health) if arguments.as_json else ("ready" if health["ready"] else "not ready"))
        if not health["ready"]:
            raise SystemExit(1)
    elif arguments.command == "cleanup":
        report = cleanup_technical_data(
            _prepare_repository(settings), settings.downloads_path,
            outbox_retention_days=settings.outbox_retention_days,
            downloads_retention_days=settings.downloads_retention_days,
            dry_run=not arguments.apply, limit=arguments.limit,
        )
        print(json.dumps(report))
    elif arguments.command == "delivery-attempts":
        print(json.dumps(unresolved_attempts(_prepare_repository(settings), limit=arguments.limit)))
    elif arguments.command == "review-delivery-attempt":
        reviewed = review_attempt(_prepare_repository(settings), arguments.attempt_id, arguments.reason)
        print(f"attempt reviewed: {reviewed}")
    elif arguments.command == "import-file":
        repository = _prepare_repository(settings)
        updater = ScheduleUpdater(
            ScheduleDownloader(
                settings.schedule_page_url,
                settings.downloads_path,
                building=1,
                proxy_url=settings.schedule_proxy_url,
                max_attempts=settings.schedule_http_max_attempts,
                retry_max_delay=settings.schedule_http_retry_max_delay,
            ),
            ExcelScheduleParser(),
            repository,
        )
        result = updater.import_local(
            arguments.file, approval_reason=arguments.approval_reason
        )
        status = "already imported" if result.skipped else "imported"
        print(
            f"{result.filename}: {status}; "
            f"lessons={result.lesson_count}; changes={len(result.changes)}"
        )
    elif arguments.command == "update":
        asyncio.run(_update(settings, approval_reason=arguments.approval_reason))
    elif arguments.command == "doctor":
        asyncio.run(_doctor(settings))
    elif arguments.command == "backup":
        repository = _prepare_repository(settings)
        backup_path = create_database_backup(
            repository,
            settings.backups_path,
            retention_count=settings.backup_retention_count,
        )
        print(f"database backup: {backup_path}")
    elif arguments.command == "retry-notifications":
        repository = _prepare_repository(settings)
        count = repository.redrive_notifications(
            arguments.import_id, arguments.destination_id, arguments.kind
        )
        print(f"notification chunks queued for retry: {count}")
    elif arguments.command == "run":
        from schedule_bot.bot import run_bot

        configure_file_logging(
            settings.database_path.parent / "logs" / "bot.log",
            retention_days=settings.log_retention_days,
        )
        asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
