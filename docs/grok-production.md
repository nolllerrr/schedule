# Grok в Docker и на сервере

Образ бота содержит Grok CLI. Версия и SHA-256 Linux-бинарников для amd64/arm64 закреплены в [`scripts/grok-cli.lock.json`](../scripts/grok-cli.lock.json). Сборка скачивает бинарник с официального хранилища xAI, проверяет размер и SHA-256; при несовпадении образ не собирается. Для обновления CLI нужно изменить lock-файл, проверить сборку и выпустить новый образ. Автообновление CLI при генерации отключено.

Compose хранит `~/.grok` пользователя `bot` в именованном volume `grok-state`. Docker создаёт его с владельцем и правами из образа. При стандартном имени проекта фактическое имя — `schedule-bot_grok-state`. Обычные обновления и пересоздание контейнера сохраняют авторизацию. Команда `docker compose down -v` удаляет этот volume и потребует нового входа.

## Первое обновление

1. Оставьте на сервере `GROK_REACTIONS_ENABLED=false`, пока не настроен вход. Не переносите локальный `.env`: токен Telegram и данные у сервера свои.
2. Разверните закоммиченный проект обычной командой `./deploy.ps1`. Сборка происходит до остановки работающего бота; переключение включает копию базы, проверку миграции и готовности, откат при отказе.
3. Войдите в Grok внутри нового контейнера и проверьте генерацию, как описано ниже.
4. В серверном `.env` включите комментарии только для разрешённых чатов и повторите `./deploy.ps1`. Это пересоздаст контейнер с новым окружением, сохранив вход в Grok.

После versioned deploy используйте текущий release из `.current-release.json`. Старый корневой `docker compose up --build` может вернуть прежние исходники.

## Вход без браузера на сервере

Команды выполняются по SSH на сервере. Найдите единственный работающий контейнер проекта:

```sh
container=$(docker ps \
  --filter label=com.docker.compose.project=schedule-bot \
  --filter label=com.docker.compose.service=bot \
  --format '{{.ID}}')
docker exec -it --user bot "$container" grok login --device-auth
```

Для другого Compose project замените `schedule-bot`. CLI покажет URL и одноразовый код. Откройте URL на своём компьютере, введите код и дождитесь подтверждения в терминале. Браузер и открытие входящих портов на сервере не требуются.

Токены сохраняются в `/home/bot/.grok/auth.json` в volume, доступном для записи пользователю `bot`. Не добавляйте этот файл в Git или образ. CLI обновляет токены автоматически; если обновление перестало работать, повторите вход. Образы, release-пакеты и обычные копии SQLite не содержат этот volume.

Вместо входа через аккаунт CLI поддерживает `XAI_API_KEY` в серверном `.env`; активная авторизация аккаунта имеет приоритет над этим ключом. Не задавайте ключ без намерения использовать соответствующий API-доступ.

## Проверка без сообщения в Telegram

```sh
docker exec --user bot "$container" grok --no-auto-update --version
docker exec -i --user bot "$container" python - <<'PY'
import asyncio
from schedule_grok import generate_grok_reaction

text = asyncio.run(generate_grok_reaction(
    "Новое расписание на пятницу: две очные пары, затем дистанционная пара.",
    enabled=True,
    timeout=120,
))
if not text:
    raise SystemExit("Grok не вернул реакцию: проверьте вход и журнал ошибок")
print(text)
PY
```

Эта проверка вызывает настоящий генератор, но не отправляет результат в чат и не запускает второй polling-процесс бота.

## Включение комментариев

В `.env` на сервере:

```dotenv
GROK_REACTIONS_ENABLED=true
GROK_CLI_PATH=/usr/local/bin/grok
GROK_TIMEOUT_SECONDS=120
GROK_REACTION_CHAT_TYPES=group,supergroup
GROK_REACTION_CHAT_IDS=-1001234567890
```

Замените пример ID на ID разрешённого группового чата; несколько ID разделяются запятой. Пустой список отключает все вызовы Grok для рассылки. Промпт поставляется с Python-пакетом и обновляется вместе с образом. По умолчанию генератор запрещает инструменты, поиск и подагентов и ограничивает ответ одной репликой.

После изменения `.env` нужен новый контейнер: `docker restart` не перечитывает окружение. Для versioned deployment повторите `./deploy.ps1` из чистого закоммиченного checkout. Для отключения задайте `GROK_REACTIONS_ENABLED=false` и повторите обновление. Сбои Grok не останавливают обычную рассылку расписания.

Официальные инструкции: [установка CLI](https://github.com/xai-org/grok-build#installing-the-released-binary), [авторизация](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md), [автоматические вызовы](https://docs.x.ai/build/cli/headless-scripting).
