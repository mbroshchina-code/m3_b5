"""Главный модуль инициализации FastAPI приложения BAG_ASSISTANT.

Строго соответствует эталону наставника, дополнен рабочим прокси-транспортом.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx  # Добавлено для твоей поддержки прокси
from openai import AsyncOpenAI

try:
    from redis.asyncio import Redis
except ImportError:
    Redis = None  # type: ignore

from app.core.config import get_settings
from app.core.exceptions import (
    LLMAuthError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.routers import chat, health, models

# Настраиваем логирование строго по эталону
logger = logging.getLogger("llm-service")
logging.basicConfig(level=logging.INFO)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управляет жизненным циклом приложения (Startup и Shutdown) по ТЗ."""
    
    # ─── 🌟 ТВОЯ ДОРАБОТКА ПРОКСИ, ВСТРОЕННАЯ В LIFESPAN ───
    # Инициализируем изолированный httpx клиент для обхода блокировок OpenAI
    http_client = None
    if settings.llm.openai_proxy_url:
        http_client = httpx.AsyncClient(proxy=settings.llm.openai_proxy_url)
        
    # Твои диагностические info-логи в консоль при старте сервера
    logger.info("OPENAI_API_KEY set: %s", bool(settings.llm.openai_api_key.get_secret_value()))
    logger.info("base_url: %s", settings.llm.base_url)
    logger.info("openai_proxy_url set: %s", bool(settings.llm.openai_proxy_url))
    # ───────────────────────────────────────────────────────

    # Инициализируем ИИ-клиент в app.state.llm (Имя по ТЗ наставника)
    app.state.llm = AsyncOpenAI(
        api_key=settings.llm.openai_api_key.get_secret_value(),
        base_url=settings.llm.base_url,
        timeout=settings.llm.request_timeout,
        max_retries=settings.llm.max_retries,
        http_client=http_client,  # Передаем прокси-клиент
    )

    # Безопасное подключение к кэш-слою Redis
    app.state.redis = None
    if Redis is not None:
        try:
            redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
            await redis_client.ping()  # Проверяем, жива ли база
            app.state.redis = redis_client
            logger.info("Успешное подключение к Redis. Кэширование активировано.")
        except Exception as e:
            logger.warning("Redis недоступен (%s) — продолжаем без кеша", e)

    yield

    # ─── SHUTDOWN: Гарантированная очистка ресурсов ───
    try:
        await app.state.llm.close()
        if http_client:
            await http_client.aclose()  # Чисто закрываем прокси сокет
    except Exception:
        pass
        
    if app.state.redis is not None:
        try:
            await app.state.redis.close()
        except Exception:
            pass


# Создаем приложение с именем из конфига (BAG_ASSISTANT)
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="FastAPI-сервис для LLM",
    lifespan=lifespan,
)

# Настройка CORS по эталону наставника
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-LLM-Cost-USD"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """HTTP-middleware для трассировки request_id и замера duration_ms."""
    request.state.request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
    request.state.llm_cost = 0.0
    request.state.llm_tokens = 0

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled", extra={"request_id": request.state.request_id})
        raise

    duration_ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-LLM-Cost-USD"] = f"{request.state.llm_cost:.6f}"
    
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.2f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request.state.request_id,
    )
    return response


_STATUS_MAP: list[tuple[type[LLMError], int, str]] = [
    (LLMRateLimitError, 429, "llm_rate_limit"),
    (LLMAuthError, 502, "llm_auth_error"),
    (LLMTimeoutError, 504, "llm_timeout"),
    (LLMContentFilterError, 400, "content_filter"),
    (LLMError, 502, "llm_error"),
]


@app.exception_handler(LLMError)
async def handle_llm_error(request: Request, exc: LLMError):
    """Маппинг кастомных доменных исключений в единый формат HTTP-ответов."""
    for cls, status, code in _STATUS_MAP:
        if isinstance(exc, cls):
            return JSONResponse(
                status_code=status,
                content={"error": {"code": code, "message": str(exc)}},
                headers={"X-Request-ID": getattr(request.state, "request_id", "")},
            )
    return JSONResponse(
        status_code=502,
        content={"error": {"code": "llm_error", "message": str(exc)}},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError):
    """Единый формат вывода ошибок валидации полей Pydantic."""
    errors = [
        {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "fields": errors}},
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )


# Подключаем роутеры дипломного проекта
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(health.router)
