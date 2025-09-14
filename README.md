# Telegram Bot with FastAPI + aiogram 3 + Postgres

Проект — Telegram-бот на aiogram v3 с FastAPI webhook, Postgres (SQLAlchemy Async + Alembic), без Redis. Логи — структурные (structlog), простые метрики в формате Prometheus.

## Стек

- Python 3.11+
- aiogram 3.*, FastAPI, uvicorn, httpx, pydantic v2, pydantic-settings
- APScheduler, SQLAlchemy[asyncio], Alembic, asyncpg
- orjson, structlog, pytest

## Структура

```
app/
  main.py                    # FastAPI, webhook, /healthz, /metrics
  config/settings.py         # Pydantic Settings (.env)
  bot/
    loader.py                # инициализация aiogram, роутеры, БД, планировщик
    webhook.py               # эндпоинт для Telegram webhook
    handlers/
      commands.py            # /start, /help, /auto_on, /auto_off
      messages.py            # входящие сообщения
    services/
      n8n_client.py          # POST в n8n с фиксированной схемой
      reply_flow.py          # общая логика: сохранение, задержка, отправка
      proactive.py           # APScheduler: рассылка каждые 1–2 часа
      history.py             # выборка недавнего контекста для n8n
      anti_spam.py           # проверка 5-секундного окна
      metrics.py             # счётчики и тайминги
      logging.py             # структурный логгер
    schemas/
      n8n_io.py              # Pydantic-модели request/response
  db/
    base.py                  # AsyncEngine/Session
    models.py                # SQLAlchemy модели
    migrations/              # Alembic (env.py, versions)
  utils/time.py              # utcnow(), random jitter helpers
scripts/
  set_webhook.py             # установка Telegram webhook
tests/
  test_antispam.py
  test_n8n_schema.py
  test_proactive_schedule.py
.env.example
requirements.txt
Dockerfile
docker-compose.yml
alembic.ini
README.md
```

## Переменные окружения (.env)

См. `.env.example`.

Важные:
- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_SECRET`
- `PUBLIC_BASE_URL` — внешний URL API (для webhook)
- `N8N_WEBHOOK_URL` — адрес n8n вебхука
- `DB_DSN` — `postgresql+asyncpg://user:pass@host:5432/db`

Проактив/задержки/антиспам задаются плоскими переменными: `AUTO_MESSAGES_DEFAULT`, `PROACTIVE_MIN_SECONDS`, `PROACTIVE_MAX_SECONDS`, `REPLY_DELAY_MIN_SECONDS`, `REPLY_DELAY_MAX_SECONDS`, `USER_MIN_SECONDS_BETWEEN_MSG`.

## Запуск без Docker

1. Поднимите Postgres и создайте БД (например `tgbot`).
2. Создайте и активируйте виртуальное окружение:
   - `python -m venv .venv && . .venv/Scripts/activate` (Windows PowerShell: `./.venv/Scripts/Activate.ps1`)
3. Установите зависимости:
   - `pip install -r requirements.txt`
4. Заполните `.env` по образцу.
5. Примените миграции:
   - `alembic upgrade head`
6. Запустите API:
   - `uvicorn app.main:app --host ${APP_HOST} --port ${APP_PORT}`
7. Установите webhook:
   - `python scripts/set_webhook.py`
   - Вебхук будет на `https://PUBLIC_BASE_URL/tg/webhook?secret=WEBHOOK_SECRET`.

## Запуск с Docker (опционально)

1. `docker-compose up -d` (поднимет `postgres` и `bot`, порт `8080` открыт).
2. Установите webhook (снаружи контейнера):
   - `python scripts/set_webhook.py`

## Поведение и логика

- Webhook принимает только текстовые сообщения. Неформатный контент: ответ «Пока поддерживается только текст».
- Входящее сохраняется в БД, `chat_state.last_user_msg_at=now()`.
- Антиспам: если от последнего < `USER_MIN_SECONDS_BETWEEN_MSG`, ответ «Слишком часто, подождите X c».
- Формируется запрос в n8n (`intent="reply"`, `context.history` — последние пары сообщений). После ответа — случайная задержка 5–10с, затем отправка.
- Ответ сохраняется в `assistant_messages`, обновляются `last_assistant_at` и `next_proactive_at` (если авто-включено).
- Проактив: APScheduler-джоб раз в 60с выбирает чаты с `auto_enabled=true` и `next_proactive_at<=now()`, генерирует `intent="proactive"` через n8n и отправляет.
- Безопасность: проверка `WEBHOOK_SECRET` через query-параметр.
- Логи — JSON со `trace_id`. Метрики — `/metrics` в формате Prometheus (счётчики и суммарные тайминги).

## Схема n8n (I/O)

Request:
```
{
  "intent": "reply" | "proactive",
  "chat": {
    "chat_id": int,
    "user_id": int | null,
    "lang": str | null,
    "username": str | null
  },
  "context": {
    "history": [ {"role": "user|assistant", "text": str, "created_at": ISO-8601}, ... ],
    "last_user_msg_at": ISO-8601 | null,
    "last_assistant_at": ISO-8601 | null
  },
  "message": { "text": str } | null,
  "trace_id": str | null
}
```

Response:
```
{
  "reply": str,
  "meta": { "model": str | null, "tokens": int | null, ... }
}
```

## Пример минимального workflow в n8n

1. Webhook-нода (POST) принимает JSON как в `Request`.
2. Function-нода формирует промпт: склеивает `context.history` и `message.text` (если intent=reply), добавляет системный промпт/персону.
3. OpenAI/OpenRouter-нода вызывает выбранную модель и возвращает ответ.
4. Function-нода маппит ответ в схему `Response` (`reply`, `meta` с `model`, `tokens`).
5. Последняя нода — Respond to Webhook с JSON.

## Настройка webhook вручную (альтернатива скрипту)

```
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://PUBLIC_BASE_URL/tg/webhook?secret=WEBHOOK_SECRET"}'
```

## Формат метрик (/metrics)

- `messages_received_total` — счётчик входящих сообщений
- `replies_sent_total` — счётчик отправленных ответов
- `proactive_sent_total` — счётчик проактивных сообщений
- `n8n_errors_total{intent=...}` — ошибки запросов в n8n
- `n8n_request_seconds_count/sum{intent=...}` — суммарные тайминги запросов в n8n
- `reply_delay_seconds_count/sum` — задержка перед отправкой ответа

## Качество

- Настроены `ruff`/`black` (см. `pyproject.toml`), минимальные типы в сервисах.
- Тесты: антиспам, валидация схемы n8n, границы планировщика.

## Полезные команды

- `pytest -q`
- `ruff check . && black --check .`
- `alembic upgrade head`
- `uvicorn app.main:app --reload --host 0.0.0.0 --port 8080`

---

Нужно — помогу подобрать простой workflow в n8n, накинуть SQL-миграции или расширить `/metrics` под Prometheus. Хотите оставить Docker-файлы как опцию или убрать их сейчас?

