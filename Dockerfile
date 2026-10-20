# Бот поддержки: Python 3.12 + uv, зависимости ставятся в /opt/venv.
#
# Сборка на VPS без реестра: чем меньше контекст, тем быстрее (см. .dockerignore).
# Миграции НЕ применяются при старте контейнера: два процесса с одним
# service и forward-only раннером устроили бы гонку. Миграции — отдельной
# командой, до первого запуска (см. README).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

# uv — статическим бинарником, без pip-возни.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

WORKDIR /app

# Слой зависимостей отдельно от кода: правка app/ не пересобирает их заново.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app/ ./app/
COPY config/ ./config/
COPY migrations/ ./migrations/

# Непривилегированный пользователь: боту root не нужен ни для чего.
RUN useradd --create-home --uid 1001 bot \
    && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app"]
