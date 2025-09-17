## Развёртывание Telegram-бота (FastAPI + aiogram v3)

Инструкция для двух вариантов: через Docker Compose (рекомендуется) и без Docker (venv + systemd). В обоих случаях потребуется настроить HTTPS (nginx) для Telegram webhook.

### Предпосылки
- Сервер: Ubuntu 22.04/24.04 (root или sudo)
- DNS: A-записи на сервер для доменов `girlbot.<домен>` и `n8n.<домен>`
- Порты 80/443 (TLS) открыты; локально бот слушает 8080

Обязательные переменные окружения (.env), см. `.env.example`:
- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_SECRET`
- `PUBLIC_BASE_URL` (публичный HTTPS URL бота, напр. `https://girlbot.noza.digital`)
- `N8N_WEBHOOK_URL` (Production URL workflow в n8n, напр. `https://n8n.noza.digital/webhook/ai-reply`)
- `DB_DSN` (DSN Postgres)

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

### 2) Клонирование и подготовка
```
cd /opt
sudo git clone <URL-репозитория> girlbot
sudo chown -R $USER:$USER girlbot
cd girlbot
cp .env.example .env
```
- Заполните `.env`. Для Compose `DB_DSN` должен ссылаться на сервис `db`:
  - `DB_DSN=postgresql+asyncpg://user:pass@db:5432/tgbot`

### 3) Запуск
```
docker compose up -d --build
```
- Логи: `docker compose logs -f bot`

### 4) nginx (reverse proxy + TLS)
```
sudo apt-get install -y nginx

# Бот (webhook) — girlbot.<домен>
sudo tee /etc/nginx/sites-available/girlbot.conf >/dev/null <<'NG'
server {
    listen 80;
    server_name girlbot.<домен>;

    # Защита статических точек
    location ~ /\.(?!well-known).* { return 404; }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
NG

# n8n — n8n.<домен>
sudo tee /etc/nginx/sites-available/n8n.conf >/dev/null <<'NG'
server {
    listen 80;
    server_name n8n.<домен>;

    location / {
        proxy_pass http://127.0.0.1:5678;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 60s;
        client_max_body_size 20m;
        add_header X-Accel-Buffering no;
    }
}
NG

sudo ln -s /etc/nginx/sites-available/girlbot.conf /etc/nginx/sites-enabled/ || true
sudo ln -s /etc/nginx/sites-available/n8n.conf /etc/nginx/sites-enabled/ || true
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# TLS (Let's Encrypt)
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d girlbot.<домен> -d n8n.<домен> --redirect
```

Примечания:
- Контейнеры `postgres` и `bot` поднимутся автоматически (бот миграции делает при старте).
- Если увидите предупреждение `the attribute version is obsolete` — можно удалить строку `version:` из `docker-compose.yml`.

### 5) Установка Telegram Webhook
- В Docker:
```
docker compose exec bot python scripts/set_webhook.py
```
- Проверка: `https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo`

### 6) Запуск n8n (если отдельно в Docker)
```
docker run -d --name n8n \
  --restart unless-stopped \
  -p 127.0.0.1:5678:5678 \
  -e N8N_HOST=n8n.<домен> \
  -e N8N_PROTOCOL=https \
  -e N8N_PORT=443 \
  -e WEBHOOK_URL=https://n8n.<домен>/ \
  -v /opt/n8n:/home/node/.n8n \
  n8nio/n8n:latest
```
- После запуска импортируйте workflow и активируйте его (Active). В `.env` у бота установите `N8N_WEBHOOK_URL` равным Production URL (`/webhook/...`).

---

## Вариант B — без Docker (venv + systemd)

### 1) Установка зависимостей
```
sudo apt-get update
sudo apt-get install -y python3.11-venv python3-pip postgresql postgresql-contrib nginx
```

### 2) Настройка БД
```
sudo -u postgres psql <<'SQL'
CREATE USER girlbot WITH PASSWORD '<пароль>';
CREATE DATABASE tgbot OWNER girlbot;
GRANT ALL PRIVILEGES ON DATABASE tgbot TO girlbot;
SQL
```

### 3) Развёртывание приложения
```
sudo mkdir -p /opt/girlbot && sudo chown -R $USER:$USER /opt/girlbot
cd /opt/girlbot
git clone <URL-репозитория> .
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
- В `.env` укажите DSN локальной БД: `DB_DSN=postgresql+asyncpg://girlbot:<пароль>@127.0.0.1:5432/tgbot`

### 4) Миграции
```
alembic upgrade head
```

### 5) systemd unit для uvicorn
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
- Аналогично разделу выше (Docker/nginx).

### 7) Установка Telegram Webhook
```
source /opt/girlbot/.venv/bin/activate
python scripts/set_webhook.py
```

### 8) n8n без Docker
```
npm i -g n8n
n8n start --port 5678
```
- Настройте nginx и `N8N_WEBHOOK_URL` как в варианте A.

---

## Проверки и обновления
- Проверка здоровья: `curl -sSf https://girlbot.<домен>/healthz` → `ok`
- Метрики: `https://girlbot.<домен>/metrics`
- Переустановка вебхука: `docker compose exec bot python scripts/set_webhook.py`
- Обновление версии:
```
git pull
docker compose up -d --build   # для Docker
# или
source .venv/bin/activate && pip install -r requirements.txt && alembic upgrade head && sudo systemctl restart girlbot.service
```

## Траблшутинг
- 502 Bad Gateway в getWebhookInfo — историческая ошибка nginx, если бот был недоступен. Если сейчас `pending_update_count=0` и всё работает, можно игнорировать или переустановить вебхук.
- 404 на `/webhook/...` n8n — workflow не активирован или неверный метод. Проверять нужно POST; включите “Active”.
- 500 `No item to return was found` — последний узел в n8n вернул 0 items. Добавьте fallback: `return [{ json: { reply: "Сервис занят, попробуйте позже", meta: {} } }];` и/или “Continue On Fail”.
- n8n медленно отвечает — по умолчанию таймаут клиента 60с. При необходимости увеличьте (сделаем настраиваемым по запросу).

