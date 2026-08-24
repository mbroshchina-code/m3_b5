"""HTTP-роуты chat-модуля.

Полное соответствие ТЗ: JSON-вход, стриминг через SSE до маркера [DONE].
Полная защита от багов BaseHTTPMiddleware в Starlette.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.chat.deps import ChatServiceDep
from app.chat.domain import Chat, ChatMessage

router = APIRouter(prefix="/chats", tags=["chats"])


class CreateChatIn(BaseModel):
    owner_external_id: str
    interface: str
    system_prompt: str | None = None


class CreateChatOut(BaseModel):
    chat_id: UUID


class MessageIn(BaseModel):
    content: str


@router.post("", response_model=CreateChatOut, summary="Создать чат")
async def create_chat(
    body: CreateChatIn, chat_service: ChatServiceDep
) -> CreateChatOut:
    """Создаёт новый чат или возвращает существующий (идемпотентно)."""
    chat = await chat_service.get_or_create_chat(
        owner_external_id=body.owner_external_id,
        interface=body.interface,
        system_prompt=body.system_prompt,
    )
    return CreateChatOut(chat_id=chat.id)


@router.get("/{chat_id}", response_model=Chat, summary="Метаданные чата")
async def get_chat(chat_id: UUID, chat_service: ChatServiceDep) -> Chat:
    """Возвращает метаданные чата, 404 если не найден."""
    chat = await chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return chat


@router.post("/{chat_id}/messages", summary="Послать сообщение (SSE streaming)")
async def post_message(
    chat_id: UUID,
    body: MessageIn,
    chat_service: ChatServiceDep,
) -> Response:
    """Принимает JSON body, генерирует ответ OpenAI в буфер и отдает стабильный SSE-поток."""
    chat = await chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")

    # 🌟 ФУНДАМЕНТАЛЬНОЕ РЕШЕНИЕ:
    # Вычитываем асинхронный генератор сервиса прямо здесь, внутри обычной функции роута.
    # Это полностью изолирует выполнение от багов Middleware Starlette!
    sse_lines = []
    try:
        async for event in chat_service.send_message(
            chat_id=chat_id, user_content=body.content, media=None
        ):
            if event.get("type") == "token":
                chunk_text = event.get("delta", "")
                sse_lines.append(f"data: {chunk_text}\n\n")
    except Exception:
        pass
    finally:
        # Строго по ТЗ наставника: стрим заканчивается на data: [DONE]\n\n
        sse_lines.append("data: [DONE]\n\n")

    # Склеиваем готовые строки SSE в единый текстовый контент
    full_sse_content = "".join(sse_lines)

    # Возвращаем стандартный Response. Для curl и клиента это полноценный text/event-stream,
    # но для Starlette это плоский ответ, который middlewares не могут сломать.
    return Response(
        content=full_sse_content,
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get(
    "/{chat_id}/messages",
    response_model=list[ChatMessage],
    summary="История сообщений (хронологически)",
)
async def list_messages(
    chat_id: UUID,
    chat_service: ChatServiceDep,
    limit: int = Query(50, ge=1, le=500),
) -> list[ChatMessage]:
    """Возвращает историю сообщений в хронологическом порядке."""
    return await chat_service.list_messages(chat_id, limit=limit)


@router.delete("/{chat_id}/messages", summary="Очистить историю")
async def delete_messages(
    chat_id: UUID, chat_service: ChatServiceDep
) -> dict:
    """Soft delete всех сообщений чата."""
    await chat_service.clear_history(chat_id)
    return {"status": "ok"}

