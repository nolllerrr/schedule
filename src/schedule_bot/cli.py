from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from aiogram import Bot

from schedule_bot.bot import run_bot
from schedule_bot.bot.session import create_telegram_session
from schedule_bot.config import Settings
from schedule_bot.downloader import ScheduleDownloader
from schedule_bot.parser import ExcelScheduleParser
from schedule_bot.repository import ScheduleRepository
from schedule_bot.services import ScheduleUpdater


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

    subparsers.add_parser("update", help="Download and import the latest workbook")
    subparsers.add_parser("doctor", help="Check the website and Telegram API")
    subparsers.add_parser("run", help="Run the Telegram bot")
    return parser


async def _update(settings: Settings) -> None:
    repository = ScheduleRepository(settings.database_path)
    updater = ScheduleUpdater(
        ScheduleDownloader(
            settings.schedule_page_url,
            settings.downloads_path,
            building=1,
        ),
        ExcelScheduleParser(),
        repository,
    )
    result = await updater.update_once()
    status = "already imported" if result.skipped else "imported"
    print(
        f"{result.parsed.source_filename}: {status}; "
        f"lessons={len(result.parsed.lessons)}; changes={len(result.changes)}"
    )


async def _doctor(settings: Settings) -> None:
    downloader = ScheduleDownloader(
        settings.schedule_page_url,
        settings.downloads_path,
        building=1,
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = build_parser().parse_args()

    if arguments.command == "parse":
        parsed = ExcelScheduleParser().parse(arguments.file)
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
    if arguments.command == "import-file":
        repository = ScheduleRepository(settings.database_path)
        updater = ScheduleUpdater(
            ScheduleDownloader(
                settings.schedule_page_url,
                settings.downloads_path,
                building=1,
            ),
            ExcelScheduleParser(),
            repository,
        )
        result = updater.import_local(arguments.file)
        status = "already imported" if result.skipped else "imported"
        print(
            f"{result.parsed.source_filename}: {status}; "
            f"lessons={len(result.parsed.lessons)}; changes={len(result.changes)}"
        )
    elif arguments.command == "update":
        asyncio.run(_update(settings))
    elif arguments.command == "doctor":
        asyncio.run(_doctor(settings))
    elif arguments.command == "run":
        asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
