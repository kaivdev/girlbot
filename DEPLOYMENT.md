## Деплой Telegram‑бота (FastAPI + aiogram v3) на сервер

Ниже два варианта: через Docker Compose (проще) и без Docker (через venv + systemd). В обоих случаях нужен публичный HTTPS‑адрес для Telegram webhook.

### Предпосылки
- Сервер: Ubuntu 22.04/24.04 (root или sudo)
- Домен/поддомен для бота, A‑запись на сервер
- Открыт 80/443 (TLS) и, если нужно, 8080 локально

Переменные окружения (.env): см. `.env.example`. Обязательно заполнить:
- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_SECRET`
- `PUBLIC_BASE_URL` (публичный HTTPS URL, куда укажем вебхук)
- `N8N_WEBHOOK_URL` (адрес workflow в n8n)
- `DB_DSN` (DSN к Postgres — формат зависит от способа деплоя)

---

## Вариант A — Docker Compose

### 1) Установка Docker/Compose
```
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

### 2) Клонирование и настройка
```
cd /opt
sudo git clone <ваш-репозиторий> girlbot
sudo chown -R $USER:$USER girlbot
cd girlbot
cp .env.example .env
```
- Заполните `.env` (для Compose `DB_DSN` внутри контейнера уже переопределяется на `postgresql+asyncpg://user:pass@db:5432/tgbot`).

### 3) Запуск
```
docker compose up -d
```
- Поднимутся: `postgres` и `bot` (порт 8080 проброшен наружу).
- Бот внутри контейнера сам применит миграции (`alembic upgrade head`) и запустит `uvicorn` (см. `Dockerfile`).

### 4) Reverse‑proxy + TLS (nginx)
```
sudo apt-get install -y nginx
sudo tee /etc/nginx/sites-available/girlbot.conf >/dev/null <<'NG'
server {
    listen 80;
    server_name your.domain.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NG
sudo ln -s /etc/nginx/sites-available/girlbot.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
- Выдайте TLS (Let’s Encrypt):
```
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.com
```

### 5) Установка Telegram Webhook
- В корне репозитория:
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # нужен httpx и python-dotenv для скрипта
python scripts/set_webhook.py
```
- Либо из контейнера:
```
docker compose exec bot python scripts/set_webhook.py
```
- Проверка: `https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo`

### 6) n8n на сервере (опционально)
Простой запуск в Docker:
```
docker run -d --name n8n \
  -p 5678:5678 \
  -e N8N_HOST=n8n.your.domain.com \
  -e N8N_PROTOCOL=https \
  -e N8N_PORT=5678 \
  -v /opt/n8n:/home/node/.n8n \
  n8nio/n8n:latest
```
- Зайдите в UI (`/` на 5678), импортируйте `n8n_workflow_ai_agent.json` из корня проекта.
- В ноде `AI Agent` укажите креды `OpenAI API` с `Base URL=https://openrouter.ai/api/v1` и API‑ключ.
- Убедитесь, что Production URL workflow совпадает с `N8N_WEBHOOK_URL` в `.env`.

---

## Вариант B — Без Docker (venv + systemd)

### 1) Установка зависимостей
```
sudo apt-get update
sudo apt-get install -y python3.11-venv python3-pip postgresql postgresql-contrib nginx
```

### 2) Создание БД/пользователя
```
sudo -u postgres psql <<'SQL'
CREATE USER girlbot WITH PASSWORD 'СЛОЖНЫЙ_ПАРОЛЬ';
CREATE DATABASE tgbot OWNER girlbot;
GRANT ALL PRIVILEGES ON DATABASE tgbot TO girlbot;
SQL
```

### 3) Развёртывание кода
```
sudo mkdir -p /opt/girlbot && sudo chown -R $USER:$USER /opt/girlbot
cd /opt/girlbot
git clone <ваш-репозиторий> .
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
- В `.env` укажите локальный DSN: `DB_DSN=postgresql+asyncpg://girlbot:ПАРОЛЬ@127.0.0.1:5432/tgbot`

### 4) Применение миграций
```
alembic upgrade head
```

### 5) systemd‑юнит для uvicorn
```
sudo tee /etc/systemd/system/girlbot.service >/dev/null <<'UNIT'
[Unit]
Description=GirlBot FastAPI (uvicorn)
After=network.target postgresql.service

[Service]
User=%i
WorkingDirectory=/opt/girlbot
Environment="PYTHONPATH=/opt/girlbot"
ExecStart=/opt/girlbot/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now girlbot.service
sudo systemctl status girlbot.service
```

### 6) nginx + TLS
- Конфигурация аналогична варианту с Docker (см. выше шаг 4).

### 7) Установка Telegram Webhook
```
source /opt/girlbot/.venv/bin/activate
python scripts/set_webhook.py
```

### 8) n8n (без Docker)
```
npm i -g n8n
n8n start --port 5678
```
- Или используйте Docker‑команду из варианта A. В `.env` обновите `N8N_WEBHOOK_URL`.

---

## Обновления и миграции
- Обновить код (в корне):
```
git pull
source .venv/bin/activate  # если без Docker
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart girlbot.service   # без Docker
# или
docker compose up -d --build            # с Docker
```

## Резервные копии БД
```
# Бэкап
pg_dump --dbname=postgresql://girlbot:ПАРОЛЬ@127.0.0.1:5432/tgbot -Fc -f /opt/backup/tgbot_$(date +%F).dump
# Восстановление
pg_restore -d postgresql://girlbot:ПАРОЛЬ@127.0.0.1:5432/tgbot /opt/backup/tgbot_YYYY-MM-DD.dump
```

## Проверка и диагностика
- Healthcheck: `curl https://your.domain.com/healthz` → `ok`
- Метрики: `curl https://your.domain.com/metrics`
- Webhook: `https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo`
- Логи (без Docker):
```
sudo journalctl -u girlbot.service -f
```
- Логи (Docker):
```
docker compose logs -f bot db
```
- Статус миграций:
```
alembic current
alembic history -v
```

## Частые проблемы
- `Bad Request: chat not found` — бот пытается писать в чат, где пользователь ещё не нажал `/start`, или бот удалён из чата. Решение: написать боту `/start` ещё раз, либо очистить `chat_state`.
- Долгая задержка — проверь `/metrics` (`n8n_request_seconds_*`). Ускорить: уменьшить `REPLY_DELAY_*` в `.env`, выбрать более быструю модель в n8n, снизить `max_tokens`.
- Проактив «не приходит» — проверь `chat_state.next_proactive_at` vs `now() AT TIME ZONE 'UTC'`. Форс: `UPDATE chat_state SET next_proactive_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 second' WHERE chat_id=<ID>;`
- Вебхук не срабатывает — заново выставить скриптом `scripts/set_webhook.py`, проверить, что `PUBLIC_BASE_URL` с валидным HTTPS.

## Безопасность
- Не публикуйте `TELEGRAM_BOT_TOKEN` и API‑ключи.
- Ограничьте доступ к `/metrics` (nginx allow/deny) или держите только во внутренней сети.
- Храните `.env` с правами 600, используйте секреты в CI/CD.

---

## Быстрый чек‑лист деплоя (Docker)
1. Установить Docker/Compose
2. Клонировать репозиторий, заполнить `.env`
3. `docker compose up -d`
4. Настроить nginx + TLS (proxy → 8080)
5. `python scripts/set_webhook.py`
6. Импортировать `n8n_workflow_ai_agent.json` в n8n и указать креды
7. Проверить `/healthz`, `/metrics`, бота в Telegram

