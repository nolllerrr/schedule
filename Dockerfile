FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock build-requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r build-requirements.lock -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

CMD ["schedule-bot", "run"]

