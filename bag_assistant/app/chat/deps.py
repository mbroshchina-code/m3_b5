"""DI для chat-модуля. Полное соответствие ТЗ."""

from collections.abc import AsyncIterator
from typing import Annotated
from pathlib import Path

from fastapi import Depends, Request
from openai import AsyncOpenAI

from app.chat.repositories.json_repo import JsonChatRepository
from app.chat.repository import ChatRepository
from app.chat.service import ChatService
from app.core.config import get_settings


async def get_repository() -> AsyncIterator[ChatRepository]:
    """Фабрика репозитория. Читает настройки строго по ТЗ."""
    settings = get_settings()
    
    # ТЗ: "json" → JsonChatRepository(base_dir=settings.chat_storage_dir)
    storage_dir = getattr(settings, "chat_storage_dir", Path("./var/chats"))
    yield JsonChatRepository(base_dir=storage_dir)


def get_llm_client(request: Request) -> AsyncOpenAI:
    """Безопасно достает уже инициализированный клиент OpenAI из состояния приложения."""
    # Пытаемся взять готовый клиент из app.state (как это сделано в М3Б4)
    llm = getattr(request.app.state, "llm", None) or getattr(request.app.state, "llm_client", None)
    if llm:
        return llm
        
    # Если сервер запущен в изоляции и состояния еще нет — берем ключ из env напрямую
    import os
    api_key = os.getenv("LLM__OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "mock-key"
    return AsyncOpenAI(api_key=api_key)


# ТЗ: repo = Depends(get_repository)
RepositoryDep = Annotated[ChatRepository, Depends(get_repository)]
# ТЗ: llm = Depends(get_llm_client)
LLMDep = Annotated[AsyncOpenAI, Depends(get_llm_client)]


def get_chat_service(
    repo: RepositoryDep,
    llm: LLMDep
) -> ChatService:
    """Собирает ChatService на основе зависимостей из ТЗ."""
    settings = get_settings()
    
    # ТЗ: chat_context_window: int = 10
    context_window = getattr(settings, "chat_context_window", 10)
    default_model = getattr(settings, "llm_default_model", "gpt-4o-mini")

    return ChatService(
        repository=repo,
        llm_client=llm,
        context_window=context_window,
        default_model=default_model,
        moderation=None,
        prompt_repo=None,
    )


# Основная зависимость для роутера
ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
