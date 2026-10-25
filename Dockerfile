FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS core

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bot

WORKDIR /app

COPY requirements.lock build-requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r build-requirements.lock -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -c "from datetime import date; from schedule_bot.domain import Lesson; from schedule_bot.card_renderer import normalize_schedule, render_card; d=date(2026,10,26); lesson=Lesson(d,'ИС-22',1,'1 пара',1,None,None,'Математика'); png=render_card(normalize_schedule([lesson],role='student',target='ИС-22',lesson_date=d)); assert png[:8] == bytes([137,80,78,71,13,10,26,10]); print('Offline card/font smoke check:', len(png), 'bytes')"

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data /home/bot/.grok \
    && printf '[cli]\nauto_update = false\n' > /home/bot/.grok/config.toml \
    && chmod 700 /home/bot/.grok \
    && chown -R bot:bot /app /home/bot
USER bot

CMD ["schedule-bot", "run"]

# Optional CLI layer. --target core never downloads or installs Grok.
# The default target preserves existing Grok-enabled production releases.
FROM core AS with-grok
USER root
COPY scripts/install_grok.py scripts/grok-cli.lock.json /tmp/grok-install/
RUN python /tmp/grok-install/install_grok.py \
    && grok --no-auto-update --version \
    && rm -r /tmp/grok-install
USER bot

