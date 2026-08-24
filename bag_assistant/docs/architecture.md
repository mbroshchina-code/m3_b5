# Системная архитектура проекта Bag Assistant

Данный документ описывает четырехуровневую архитектуру приложения, потоки данных при обработке запросов, а также механизмы обеспечения отказоустойчивости и оптимизации затрат/задержек.

## 1. Четырехуровневая диаграмма архитектуры (Mermaid Architecture Diagram)

Схема разделена на 4 изолированных слоя (`Gateway`, `Service`, `LLM Layer`, `Data Layer`). На ней явно выделены точки отказоустойчивости (Circuit Breaker, Fallback Chain), узел Cache-Aside и прокси-интеграция.

```mermaid
graph TD
    classDef gatewayStyle fill:#e1f5fe,stroke:#0288d1,stroke-width:2px;
    classDef serviceStyle fill:#e8f5e9,stroke:#388e3c,stroke-width:2px;
    classDef llmStyle fill:#fff3e0,stroke:#f57c00,stroke-width:2px;
    classDef dataStyle fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;
    classDef extStyle fill:#eceff1,stroke:#455a64,stroke-width:2px,stroke-dasharray: 5 5;

    %% ==========================================
    %% 1. СЛОЙ GATEWAY (Входной слой)
    %% ==========================================
    subgraph Layer_1_Gateway [1. Layer: API Gateway]
        direction TB
        NGINX[Reverse Proxy: Nginx]:::gatewayStyle
        AUTH[Auth: Middleware]:::gatewayStyle
        RL[Rate Limiter: enforce_rate_limit]:::gatewayStyle
        
        NGINX --> AUTH --> RL
    end

    %% ==========================================
    %% 2. СЛОЙ SERVICE (Бизнес-логика приложения)
    %% ==========================================
    subgraph Layer_2_Service [2. Layer: Application Service]
        direction TB
        Router[FastAPI Router: routes.py]:::serviceStyle
        ChatServ[ChatService: service.py]:::serviceStyle
        ModServ[ModerationService]:::serviceStyle
        
        Router --> ModServ
        ModServ -->|Allowed| ChatServ
    end

    %% ==========================================
    %% 3. СЛОЙ LLM (Интеллектуальный слой & Провайдеры)
    %% ==========================================
    subgraph Layer_3_LLM [3. Layer: LLM Orchestration & Cache]
        direction TB
        CacheAside{Cache-Aside: Redis<br/>TTL: 1h (3600s)<br/>Key: hash_md5(model, messages, temp)}:::llmStyle
        LiteLLM[Orchestrator: LiteLLM Proxy<br/>Circuit Breaker Точка контроля]:::llmStyle
        OpenAI[Primary: OpenAI GPT-4o-mini]:::llmStyle
        OpenRouter[Secondary Fallback: OpenRouter GPT-OSS]:::llmStyle
        Ollama[Tertiary Fallback: Ollama Qwen 2.5]:::llmStyle
        
        CacheAside -->|1. Cache Miss| LiteLLM
        LiteLLM -->|2. Try Primary| OpenAI
        LiteLLM -.->|3. Circuit Breaker Fallback| OpenRouter
        LiteLLM -.->|4. Offline Fallback| Ollama
    end

    %% ==========================================
    %% 4. СЛОЙ DATA (Хранение данных и состояние)
    %% ==========================================
    subgraph Layer_4_Data [4. Layer: Data & Infrastructure]
        direction TB
        RepoFactory[Repository Factory: deps.py]:::dataStyle
        JsonRepo[JsonChatRepository: json_repo.py]:::dataStyle
        PgRepo[PostgresChatRepository: pg_repo.py]:::dataStyle
    end

    %% ВНЕШНИЕ СЕРВИСЫ
    Ext_Client[🧑‍💻 Client: Telegram Bot / CLI / Web]
    Ext_Redis[🛢️ Redis Stack]:::extStyle
    Ext_Postgres[🐘 PostgreSQL DB]:::extStyle

    %% СВЯЗИ И ПОТОКИЗАПРОСОВ
    Ext_Client -->|HTTP Request| NGINX
    RL -->|Check Limits| Ext_Redis
    RL -->|Forward Request| Router

    ChatServ -->|Call completions.create| CacheAside
    CacheAside -.->|Cache Hit: Return Token Stream| ChatServ
    
    ChatServ --> RepoFactory
    RepoFactory -->|CHAT_REPOSITORY = json| JsonRepo
    RepoFactory -->|CHAT_REPOSITORY = postgres| PgRepo
    
    JsonRepo -->|Append-Only| LocalDisk[(Local Disk: JSONL Files)]
    PgRepo -->|SQL Transactions| Ext_Postgres

    %% Style links for specific critical paths
    %% Redis latency-critical path
    linkStyle 4 stroke:#ff1744,stroke-width:2px;
    %% OpenAI cost-critical path
    linkStyle 7 stroke:#2979ff,stroke-width:2px;
```

## 2. Паттерн Cache-Aside (Логика кэширования ответов)

Для минимизации затрат на повторные однотипные запросы перед LLM-слоем развернут узел **Cache-Aside** на базе Redis [1.1].

*   **Сборка ключа кэша**: Ключ генерируется как детерминированный `MD5-хэш` от конфигурационных параметров запроса, склеенных в единую строку [1.1]:
    `hash(model + json.dumps(messages) + str(temperature) + str(max_tokens))` [1.1]. Это гарантирует, что если пользователь отправляет тот же контекст с теми же настройками, он мгновенно получит ответ из кэша [1.1].
*   **Параметр времени жизни (TTL)**: Установлен `TTL = 3600 секунд` (1 час) [1.1]. Данный интервал оптимален для сессий техподдержки, предотвращая забивание оперативной памяти Redis устаревшим контекстом.

## 3. Оркестрация через LiteLLM Proxy

### Обоснование выбора: «Брать готовый LiteLLM» против «Писать роутер самим»
Вместо написания собственного велосипеда для каскадного переключения моделей (Fallback chain) на чистом Python, в проект внедрен официальный **LiteLLM Proxy Server** [1.1].

*   **Почему взят готовый LiteLLM**:
    1.  **Встроенный Circuit Breaker**: Сервер из коробки умеет отслеживать коды ошибок (403, 429, 5xx) и автоматически «размыкать цепь», перенаправляя трафик на резервного провайдера без перезапуска основного FastAPI приложения [1.1].
    2.  **Стандартизация OpenAI**: LiteLLM предоставляет единый wire-совместимый интерфейс OpenAI Completions [1.1]. Наш `ChatService` отправляет запросы в единую точку, не зная, какая модель (облачная или локальная Ollama) отвечает за генерацию в данный момент. Это радикально упрощает кодовую базу.

### Конфигурация `docs/litellm/config.yaml`
Для работы прокси-слоя подготовлен и закоммичен следующий конфигурационный файл:

```yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: "os.environ/OPENAI_API_KEY"
      tpm: 50000
      rpm: 100

  - model_name: claude-haiku
    litellm_params:
      model: anthropic/claude-3-5-haiku-20241022
      api_key: "os.environ/ANTHROPIC_API_KEY"

  - model_name: local-fallback
    litellm_params:
      model: ollama/llama3
      api_base: "http://localhost:11434"

router_settings:
  routing_strategy: failover
  set_verbose: true
  allowed_fails: 3
  cooldown_time: 30
```

## 4. Спецификация сетевых вызовов

*   **Latency-Critical**: Проверка лимитов в Redis на слое Gateway (< 5 мс) [1.1].
*   **Cost-Critical**: Фиксация транзакций в PostgreSQL DB и вызовы OpenAI API GPT-4o-mini [1.1].
