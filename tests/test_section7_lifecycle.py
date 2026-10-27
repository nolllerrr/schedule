import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SetMyCommands

from schedule_bot.bot import app

pytest_plugins = ("test_stage4_background_jobs",)
pytestmark = pytest.mark.asyncio


async def test_configure_commands_network_failure_retries_then_polls(runtime):
    error = TelegramNetworkError(method=SetMyCommands(commands=[]), message="offline")
    app.configure_commands.side_effect = [error, None]
    await app.run_bot(runtime.settings)
    assert app.configure_commands.await_count == 2
    runtime.dispatcher.start_polling.assert_awaited_once()
    assert runtime.scheduler.stopped
    runtime.bot.session.close.assert_awaited_once()


async def test_signal_reaches_running_listener_and_worker_is_stopped(runtime):
    event = asyncio.Event()
    runtime.bot.id = 999

    async def update(*args, **kwargs):
        if kwargs.get("wait_for_lock"):
            event.set()

    runtime.update.side_effect = update

    async def polling(bot, **kwargs):
        repo = kwargs["repository"]
        repo.save_chat_profile(-100, chat_type="supergroup", chat_title="signal",
                               target="A", configured_by=1)
        message = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"),
                                  from_user=SimpleNamespace(id=5, is_bot=True, username="source"),
                                  message_id=1, text="changed", caption=None, document=None)
        await runtime.listeners[0].receive(message, bot)
        await asyncio.wait_for(event.wait(), 5)

    runtime.dispatcher.start_polling.side_effect = polling
    await app.run_bot(runtime.settings)
    assert sum(bool(call.kwargs.get("wait_for_lock")) for call in runtime.update.await_args_list) == 1
    assert runtime.scheduler.stopped
    runtime.bot.session.close.assert_awaited_once()


@pytest.mark.xfail(strict=True, reason="startup scheduler failure bypasses worker and session cleanup")
@pytest.mark.parametrize("resource", ["worker", "session", "card_service"])
async def test_startup_scheduler_failure_closes_already_created_worker(runtime, monkeypatch, resource):
    tasks = []
    async def running_worker(self):
        tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    monkeypatch.setattr(app.GrokReactionWorker, "run", running_worker)
    error = RuntimeError("scheduler registration failed")
    runtime.scheduler.add_job = lambda *args, **kwargs: (_ for _ in ()).throw(error)
    try:
        with pytest.raises(RuntimeError, match="scheduler registration failed") as caught:
            await app.run_bot(runtime.settings)
        assert caught.value is error
        await asyncio.sleep(0)
        if resource == "worker":
            assert not tasks or all(task.done() for task in tasks)
        elif resource == "session":
            runtime.bot.session.close.assert_awaited_once()
        else:
            app.close_card_service.assert_awaited_once()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
