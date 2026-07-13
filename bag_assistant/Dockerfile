# syntax=docker/dockerfile:1.7

# ==============================================================================
# STAGE 1: BUILDER (Изолированная сборка зависимостей в виртуальном окружении)
# ==============================================================================
FROM python:3.12-slim-bookworm AS builder

# Настройка переменных uv для максимальной производительности компиляции
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_HTTP_TIMEOUT=60

# Копируем бинарники uv официального релиза в системный путь builder-стадии
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /bin/

WORKDIR /app

# Шаг 1: Сначала копируем только манифест пакетов для правильного кэширования слоев.
# Если pyproject.toml не менялся, Docker не будет скачивать библиотеки заново
COPY pyproject.toml ./

# Собираем зависимости во временном кэш-монтировании без установки самого проекта
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Шаг 2: Копируем код приложения (включая папку app/prompts с базой багов)
COPY app/ ./app/

# Выполняем финальную синхронизацию и жесткую фиксацию проекта
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# ==============================================================================
# STAGE 2: RUNTIME (Минимальный боевой образ без лишнего мусора и uv компиляторов)
# ==============================================================================
FROM python:3.12-slim-bookworm

# Создаем безопасного non-root пользователя appuser
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Копируем из builder-стадии ТОЛЬКО готовое скомпилированное окружение и код
# Принудительно передаем права владения нашему безопасному пользователю
COPY --from=builder --chown=appuser:appuser /app /app

# Активируем виртуальное окружение .venv внутри путей контейнера контейнера
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Переключаемся на non-root пользователя для защиты от уязвимостей взлома хоста
USER appuser

# Открываем порт для сетевого моста
EXPOSE 8000

# Healthcheck через стандартную библиотеку Python
# Опрашивает нашу готовую ручку /ready раз в 30 секунд
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://localhost:8000/ready', timeout=3).status == 200 else sys.exit(1)" \
    || exit 1

# EXEC-форма запуска uvicorn на адресе 0.0.0.0
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
