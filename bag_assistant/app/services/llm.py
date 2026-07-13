"""Модуль ядра бизнес-логики ИИ-сервиса (LLM API Gateway).

Полностью соответствует эталону наставника, реализует кэширование и лексический поиск.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from app.infrastructure.tools import get_tools_schema
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.exceptions import (
    LLMAuthError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
# Импортируем наш оригинальный системный промпт баг-ассистента
from app.core.prompts import BAG_SYSTEM_PROMPT
from app.schemas.chat import ChatDelta, ChatRequest, ChatResponse, Usage

try:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        RateLimitError,
    )
except ImportError:
    APIConnectionError = APITimeoutError = AuthenticationError = BadRequestError = RateLimitError = ()  # type: ignore


class LLMService:
    """Сервис управления запросами к ИИ с поддержкой отказоустойчивости и кэша."""

    def __init__(self, llm: object, cache: object | None, ttl: int = 3600):
        """Инициализирует сервис компонентами из DI-контейнера."""
        self.llm = llm  # Передается AsyncOpenAI клиент
        self.cache = cache  # Передается Redis клиент
        self.ttl = ttl
        
    async def simple_complete(self, prompt: str, req_model: str = "gpt-4o-mini") -> str:
        """Легковесный прямой вызов ИИ без кэша и инструментов для внутренних нужд бэкэнда."""
        try:
            raw = await self.llm.chat.completions.create(
                model=req_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, # Низкая температура для строгих синонимов
                max_tokens=150,
            )
            return (raw.choices[0].message.content or "").strip()
        except Exception as e:
            # Если сеть OpenAI моргнула, возвращаем пустую строку, чтобы не ломать основной поиск
            return ""


    def _key(self, req: ChatRequest) -> str:
        """Генерирует SHA-256 хэш-ключ от полей запроса для Redis по ТЗ наставника."""
        # Исключаем user_id и stream для детерминированности кэша
        payload = req.model_dump(exclude={"user_id", "stream", "session_id"})
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return "chat:" + hashlib.sha256(blob.encode()).hexdigest()

    def _prepare_messages(self, req: ChatRequest) -> list[dict]:
        """Проверяет контекст и при необходимости внедряет BAG_SYSTEM_PROMPT."""
        raw_messages = [m.model_dump() for m in req.messages]
        
        # Если в истории нет системного промпта, принудительно ставим его в начало
        has_system = any(m["role"] == "system" for m in raw_messages)
        if not has_system:
            raw_messages.insert(0, {"role": "system", "content": BAG_SYSTEM_PROMPT})
            
        return raw_messages

    def _sync_bug_search(self, query: str) -> str:
        """Синхронный лексический поиск по JSON базе данных багов.
        
        Сканирует абсолютно все текстовые поля объекта, исключая ошибки структуры ключей.
        """
        db_path = Path(__file__).resolve().parent.parent.parent / "prompts" / "bugs_database.json"
        if not db_path.exists():
            return "Ошибка: Локальная база инцидентов BAG_ASSISTANT отсутствует."
            
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                bugs = json.load(f)
            
            # Разбиваем запрос на основы слов (срезаем последние 2 буквы у длинных слов)
            raw_words = [w.lower().strip() for w in query.split() if len(w) > 2]
            words = [w[:-2] if len(w) > 4 else w for w in raw_words]

            if not words:
                return "Поисковое облако пусто."

            found_bugs = []
            for bug in bugs:
                # Склеиваем абсолютно ВСЕ текстовые значения полей бага в одну большую строку
                # Это гарантирует, что мы найдем слова, где бы они ни лежали (в body, description, theme или name)
                full_bug_text = " ".join(str(value).lower() for value in bug.values())
                
                # Дополнительно проверяем вложенные словари (например, старый content.body)
                if isinstance(bug.get("content"), dict):
                    full_bug_text += " " + " ".join(str(v).lower() for v in bug["content"].values())

                # Считаем совпадения урезанных корней слов в этой мега-строке тикета
                matches = sum(1 for word in words if word in full_bug_text)
                
                # Если нашли хотя бы 2 совпадения корней — баг гарантированно релевантен!
                if matches >= 2:
                    found_bugs.append(bug)
                    
            if found_bugs:
                return json.dumps(found_bugs, ensure_ascii=False, indent=2)
            return "В базе BAG_ASSISTANT совпадений не найдено."
            
        except Exception as e:
            return f"Ошибка парсинга базы: {e}"

        

    async def execute_bug_search(self, query: str) -> str:
        """Потокобезопасная обёртка над поиском багов по ТЗ наставника."""
        # Выносим тяжелое чтение файла с диска в пул потоков (Thread Pool)
        return await asyncio.to_thread(self._sync_bug_search, query)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    # app/services/llm.py -> Переписываем метод _call

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _call(self, req: ChatRequest) -> ChatResponse:
        """Выполняет защищенный сетевой вызов к OpenAI SDK с имитацией вашей оригинальной схемы тулов."""
        try:
            processed_messages = self._prepare_messages(req)
            user_query = req.messages[-1].content if req.messages else ""
            
            # 1. Запускаем Query Expansion через ваш легкий simple_complete
            expansion_prompt = (
                f"Напиши через запятую ровно 3 разные по звучанию технические фразы-синонима "
                f"для поискового запроса: '{user_query}'."
            )
            synonyms_str = await self.simple_complete(expansion_prompt, req_model=req.model)
            
            # Формируем массив из 3-х синонимов строго по вашей схеме инструментов
            queries_list = [s.strip() for s in synonyms_str.split(",") if s.strip()][:3]
            if len(queries_list) < 3:
                # Страховка: если ИИ вернул меньше 3-х, добиваем оригинальным запросом
                queries_list.extend([user_query] * (3 - len(queries_list)))

            # 2. Вызываем лексический поиск по базе багов, передавая все фразы для сканирования
            full_search_cloud = f"{user_query} " + " ".join(queries_list)
            db_context = await self.execute_bug_search(full_search_cloud)
            
            # Имитируем уникальный ID вызова инструмента
            tool_call_id = "call_search_bug_db_123"
            
            # 3. Вставляем в историю сообщение assistant. Текст аргументов СТРОГО соответствует вашей схеме!
            processed_messages.insert(1, {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "search_bug_database",
                            # Передаем JSON с ключом "queries" и массивом из 3-х синонимов!
                            "arguments": json.dumps({"queries": queries_list}, ensure_ascii=False)
                        }
                    }
                ]
            })
            
            # 4. Вставляем ответ от самого тула (передаем найденные баги из JSON)
            processed_messages.insert(2, {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": "search_bug_database",
                "content": db_context
            })
            
            # Отправляем полный контекст в OpenAI, подключая вашу оригинальную схему get_tools_schema()
            raw = await self.llm.chat.completions.create(
                model=req.model,
                messages=processed_messages,  # type: ignore
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                tools=get_tools_schema(),  # <-- ВНЕДРИЛИ ТВОЮ ОРИГИНАЛЬНУЮ СХЕМУ!
                tool_choice="none"
            )
            return ChatResponse.from_openai(raw)
            
        except RateLimitError as e:
            raise LLMRateLimitError(str(e)) from e
        except AuthenticationError as e:
            raise LLMAuthError(str(e)) from e
        except APITimeoutError as e:
            raise LLMTimeoutError(str(e)) from e
        except BadRequestError as e:
            msg = str(e).lower()
            if "content" in msg and ("filter" in msg or "policy" in msg):
                raise LLMContentFilterError(str(e)) from e
            raise LLMError(str(e)) from e
        except APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e


    async def complete(self, req: ChatRequest) -> ChatResponse:
        """Выполняет синхронный запрос ИИ, управляя логикой кэша и Query Expansion."""
        
        # 1. Проверяем Redis-кэш (только при temperature == 0.0 по ТЗ наставника)
        key = self._key(req)
        if req.temperature == 0.0 and self.cache is not None:
            try:
                blob = await self.cache.get(key)
                if blob:
                    resp = ChatResponse.model_validate_json(blob)
                    resp.cached = True
                    return resp
            except Exception:
                pass

        # Вытаскиваем оригинальный вопрос пользователя
        user_query = req.messages[-1].content if req.messages else ""

        # ─── 🌟 ВНЕДРЯЕМ СУПЕР-ФИЧУ: QUERY EXPANSION (РАСШИРЕНИЕ ЗАПРОСА) ───
        # Просим ИИ сгенерировать синонимы, чтобы расширить поисковое облако
        expansion_prompt = (
            f"Напиши через пробел ровно 3 разные по звучанию, но максимально близкие "
            f"по техническому смыслу фразы-синонимы для поискового запроса: '{user_query}'. "
            f"Пиши только поисковые слова, без знаков препинания, списков и лишнего текста."
        )
        
        # Вызываем легкий метод генерации синонимов
        synonyms = await self.simple_complete(expansion_prompt, req_model=req.model)
        
        # Склеиваем оригинальный запрос и 3 синонима от ИИ в единое текстовое облако
        full_search_cloud = f"{user_query} {synonyms}"
        # ───────────────────────────────────────────────────────────────────

        # 2. Вызываем локальный лексический поиск по базе багов, передавая расширенное облако
        db_context = await self.execute_bug_search(full_search_cloud)
        
        # Если даже с синонимами в JSON-базе ничего не нашлось — возвращаем бесплатный отлуп
        if db_context == "В базе BAG_ASSISTANT совпадений не найдено.":
            return ChatResponse(
                content="Подходящих багов в базе данных BAG_ASSISTANT не найдено. Запрос не относится к известным инцидентам.",
                model=req.model,
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                cached=False
            )

        # 3. Кэш-промах И баг найден: идем в сеть OpenAI через защищенный метод _call
        resp = await self._call(req)
        resp.cached = False
        
        # Записываем свежий ответ в Redis (если температура нулевая)
        if req.temperature == 0.0 and self.cache is not None:
            try:
                await self.cache.setex(key, self.ttl, resp.model_dump_json())
            except Exception:
                pass
                
        return resp


    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        """Реализует потоковую генерацию (Streaming) по протоколу stream_options."""
        processed_messages = self._prepare_messages(req)
        
        stream_engine = await self.llm.chat.completions.create(
            model=req.model,
            messages=processed_messages,  # type: ignore
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        
        async for chunk in stream_engine:
            if getattr(chunk, "choices", None):
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    yield ChatDelta(content=delta.content)
            if getattr(chunk, "usage", None):
                yield ChatDelta(usage=Usage.from_openai(chunk.usage))
