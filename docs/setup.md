# Установка и запуск

Для запуска используются исходники проекта и `.env` в его рабочем каталоге. Выберите Docker либо локальный Python. Поддерживается один работающий процесс бота на одну базу.

## Получить проект и настройки

```sh
git clone https://github.com/nolllerrr/schedule.git
cd schedule
```

Скопируйте `.env.example` в `.env`: в PowerShell — `Copy-Item .env.example .env`, в Linux — `cp .env.example .env`.

Минимум для запуска:

```dotenv
BOT_TOKEN=токен_от_BotFather
```

Для `/update`, `/stats` и получения сообщений о сбоях добавьте собственный Telegram ID:

```dotenv
ADMIN_TELEGRAM_IDS=123456789
```

Можно перечислить несколько ID через запятую. Каждый администратор должен открыть личный чат с ботом и отправить `/start`, чтобы бот мог ему писать.

Для нового экземпляра можно сразу задать отдельный `ANALYTICS_SALT` и сохранять его при обновлениях. Без явного значения первый runtime-запуск создаёт постоянный файл ключа рядом с базой. При обновлении существующего экземпляра учитывайте [перенос и ротацию ключей](configuration.md#хранение-копии-и-статистика), чтобы сохранить возможность удаления прежних событий. Остальные значения и настройка прокси — в [справочнике](configuration.md).

## Docker Compose

Нужны установленные Docker и Compose. Команды выполняются в каталоге проекта:

```sh
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 bot
```

Контейнер сам проверяет источник после запуска. Загружать Excel вручную для обычного старта не требуется. Затем откройте бота в Telegram и выберите профиль через `/start`.

Рабочие данные сохраняются через `./data:/app/data`; Compose перезапускает контейнер согласно `unless-stopped`. При стандартной конфигурации база, загрузки, копии и файловый журнал находятся в `./data` на сервере. Изменяя пути в `.env`, убедитесь, что они попадают в постоянный volume.

Образ запускает приложение под пользователем `bot`. Каталог `./data` на хосте должен быть доступен ему для записи. При `PermissionError` проверьте права каталога; UID/GID можно узнать через `docker compose run --rm --no-deps bot id` и назначить подходящего владельца или ACL. В текущем Dockerfile задан UID `1000`.

Проверка доступа к странице источника и Telegram API:

```sh
docker compose exec bot schedule-bot doctor
```

Проверка локальной готовности процесса без запросов к Telegram и источнику:

```sh
docker compose exec bot schedule-bot health --json
```

Она проверяет свежий heartbeat polling, worker и планировщика. Состояние источника и давность расписания оцениваются отдельно. Compose использует эту же команду как healthcheck.

Для обычной остановки: `docker compose stop bot`. Обновление версии и откат описаны в [обслуживании](operations.md#обновление-и-откат).

Настройка комментариев в Docker, сохранение авторизации и вход на сервере описаны в [Grok в продакшене](grok-production.md).

## Локальный запуск в Windows

Нужен Python 3.14 или новее. Для разработки и CI версия 3.14 закреплена в `.python-version`; Docker использует Python 3.14.9. Команды PowerShell не требуют активации окружения:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r build-requirements.lock -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m schedule_bot run
```

До команды `run` заполните `.env`. Закреплённые зависимости включают `tzdata` для Windows.

## Локальный запуск в Linux

Нужен Python 3.14+ с поддержкой `venv`:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r build-requirements.lock -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m schedule_bot run
```

Во всех вариантах рабочий каталог определяет расположение `.env` и относительных путей данных. В установленном окружении доступна также команда `schedule-bot`; запуск через `python -m schedule_bot` выполняет тот же CLI.

## Отправка обновлений из Windows

`deploy.ps1` использует [`scripts/release_tools.py`](../scripts/release_tools.py). Нужны подготовленный сервер с Python 3.14+, Docker/Compose, SSH-доступом, своим `.env` и `data/`, а также один уже работающий контейнер бота. Первоначальную настройку сервера скрипт не выполняет.

1. Скопируйте `.deploy.example.json` в `.deploy.json` и заполните `server`, `port`, `remoteDir`, `keyPath`; необязательный `project` задаёт существующий Compose project.
2. Установите зависимости разработки по [инструкции](architecture.md#проверка-изменений).
3. Подготовьте чистый закоммиченный checkout. `./deploy.ps1 -Plan` показывает локальный план без соединения с сервером; `./deploy.ps1 -PackageDirectory ../release-artifact` создаёт локальный пакет.
4. `./deploy.ps1` запускает проверки и развёртывание выбранного коммита. `-SkipTests` пропускает локальные тесты, а не проверки самого переключения.

Пакет содержит проверенный манифест файлов и собирается на сервере в новом каталоге release. Перед переключением сохраняются прежний образ и проверенная копия базы; миграция репетируется на отдельной копии без сети, затем проверяются heartbeat нового процесса и отсутствие рестартов. При отказе переключения предусмотрен возврат образа и снимка базы.

Пути текущего release хранятся в `.current-release.json`. После такого развёртывания не запускайте старый корневой `docker compose up --build`: он использует прежнее дерево исходников. Параметры отката, блокировки и ограничения описаны в [операционном руководстве](../scripts/OPERATIONS.md). Настройки и рабочие данные с компьютера на сервер не копируются.

## Ручной деплой из GitHub

Вкладка **Actions → Deploy production → Run workflow** запускает тот же release-механизм на выбранном коммите `main`. Push сам по себе не развёртывает бота. Перед подключением к серверу workflow повторно проверяет зависимости, Ruff, тесты, сборку и офлайн-запуск Docker-образа. Одновременные запуски продакшн-деплоя блокируются.

Один раз создайте в **Settings → Environments** окружение `production`. В его **Secrets** добавьте `PRODUCTION_SSH_PRIVATE_KEY` — приватный ключ пользователя, настроенного в `.deploy.json`. В **Variables** добавьте `PRODUCTION_SERVER` (`user@host`), `PRODUCTION_REMOTE_DIR` (каталог проекта на сервере) и `PRODUCTION_KNOWN_HOSTS` (строку публичного ключа SSH-сервера, сверенную с доверенным отпечатком). Дополнительно можно задать `PRODUCTION_PORT` (по умолчанию `22`) и `PRODUCTION_COMPOSE_PROJECT` (по умолчанию `schedule-bot`). Workflow запускается только с `main`; ключ сервера проверяется через `known_hosts`. Для запуска одним нажатием не включайте в этом окружении правило обязательного подтверждения ревьюером.

На сервере по-прежнему должны быть установлены Python 3.14+ и Docker/Compose, настроены `.env` и постоянная `data/`, и уже должен работать один контейнер бота. Сначала добавьте workflow в `main` и заполните значения production-окружения; до этого запустить рабочий деплой из GitHub нельзя.

[Оглавление](README.md)
