"""Бизнес-логика чат-модуля.

Оркестратор: history -> context strategy -> LLM -> save.
"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID
import tiktoken
from fastapi import UploadFile

from app.chat.domain import Chat, ChatMessage
from app.chat.repository import ChatRepository

logger = logging.getLogger("llm-service.chat")

# 🌟 ВОЗВРАЩАЕМ ФУНКЦИЮ ПОДСЧЕТА строго по ТЗ (o200k_base и ChatML overhead):
def count_tokens(messages: list) -> int:
    """Подсчет токенов через o200k_base с учетом ChatML overhead (+4 на сообщение, +2 итого)."""
    try:
        encoding = tiktoken.get_encoding("o200k_base")
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")

    num_tokens = 0
    for message in messages:
        num_tokens += 4  # Overhead на структуру сообщения
        # Если пришел доменный объект ChatMessage
        if hasattr(message, "content") and hasattr(message, "role"):
            num_tokens += len(encoding.encode(message.content))
            num_tokens += len(encoding.encode(message.role))
        # If dict format
        elif isinstance(message, dict):
            num_tokens += len(encoding.encode(message.get("content", "")))
            num_tokens += len(encoding.encode(message.get("role", "")))
            
    num_tokens += 2  # Overhead на финальный ответ ассистента
    return num_tokens

class ChatService:
    def __init__(
        self,
        repository: ChatRepository,
        llm_client,
        context_window: int = 10,
        default_model: str = "gpt-4o-mini",
        moderation=None,
        prompt_repo=None,
    ):
        self.repository = repository
        self.llm_client = llm_client
        self.context_window = context_window
        self.default_model = default_model
        self.moderation = moderation
        self.prompt_repo = prompt_repo

    @staticmethod
    def _message_content_for_llm(m: ChatMessage) -> str:
        """Возвращает текстовый контент сообщения."""
        return m.content

    def _build_context(
        self,
        chat: Chat,
        history: list[ChatMessage],
        system_prompt_body: str | None = None,
    ) -> list[dict]:
        """Sliding window: system_prompt + последние N сообщений."""
        messages: list[dict] = []
        effective_prompt = system_prompt_body or chat.system_prompt
        if effective_prompt:
            messages.append({"role": "system", "content": effective_prompt})
        for m in history:
            messages.append(
                {"role": m.role, "content": self._message_content_for_llm(m)}
            )
        return messages

    async def create_chat(
        self, owner_external_id: str, interface: str, system_prompt: str | None = None
    ) -> Chat:
        return await self.repository.create_chat(
            owner_external_id=owner_external_id,
            interface=interface,
            system_prompt=system_prompt,
        )

    async def get_chat(self, chat_id: UUID) -> Chat | None:
        return await self.repository.get_chat(chat_id)

    async def get_or_create_chat(
        self, owner_external_id: str, interface: str, system_prompt: str | None = None
    ) -> Chat:
        return await self.repository.get_or_create_chat(owner_external_id, interface)

    async def list_messages(self, chat_id: UUID, limit: int = 50) -> list[ChatMessage]:
        return await self.repository.list_messages(chat_id, limit=limit)

    async def clear_history(self, chat_id: UUID) -> None:
        await self.repository.soft_delete_messages(chat_id)

    async def check_input(self, content: str, owner_external_id: str | None = None):
        """Прослойка модерации. Возвращает объект с полем allowed=True."""
        class DefaultModResult:
            allowed = True
            categories = []
            layer = "passed"
        return DefaultModResult()

    async def send_message(
        self, chat_id: UUID, user_content: str, media: UploadFile | None = None
    ) -> AsyncIterator[dict]:
        """Полный цикл обработки сообщения пользователя строго по ТЗ."""
        # 1. Сохраняем user-сообщение
        user_message = ChatMessage(
            chat_id=chat_id,
            role="user",
            content=user_content,
        )
        await self.repository.append_message(chat_id, user_message)

        # 2. Загрузим чат
        chat = await self.repository.get_chat(chat_id)
        if chat is None:
            raise ValueError(f"chat {chat_id} not found")

        # 3. История + контекст (N = self.context_window)
        history = await self.repository.list_messages(
            chat_id, limit=self.context_window
        )
        messages = self._build_context(chat, history, chat.system_prompt)

        # 4. Стримим
        buffer = ""
        stream = await self.llm_client.chat.completions.create(
            model=self.default_model,
            messages=messages,
            stream=True,
        )

        try:
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    buffer += content
                    yield {"type": "token", "delta": content}
        except Exception as exc:
            logger.warning("stream interrupted chat_id=%s err=%s", chat_id, exc)
            if buffer:
                await self.repository.append_message(
                    chat_id,
                    ChatMessage(chat_id=chat_id, role="assistant", content=buffer),
                )
            raise

        # 5. Успешное завершение — сохраняем накопленный ответ
        if buffer:
            await self.repository.append_message(
                chat_id,
                ChatMessage(chat_id=chat_id, role="assistant", content=buffer),
            )
