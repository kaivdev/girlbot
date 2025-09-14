# n8n на сервере с доменом (поддомен + nginx + TLS)

## Шаги
- DNS: добавьте A/AAAA запись `n8n` на IP сервера (например, `n8n.your.domain.com`).
- Фаервол: откройте 80/443 (`sudo ufw allow "Nginx Full"`).
- Docker n8n (за nginx):
  ```bash
  docker run -d --name n8n \
    --restart unless-stopped \
    -p 127.0.0.1:5678:5678 \
    -e N8N_HOST=n8n.noza.digital \
    -e N8N_PROTOCOL=https \
    -e N8N_PORT=443 \
    -e WEBHOOK_URL=https://n8n.your.domain.com/ \
    -e N8N_BASIC_AUTH_ACTIVE=true \
    -e N8N_BASIC_AUTH_USER=admin \
    -e N8N_BASIC_AUTH_PASSWORD=change_me \
    -v /opt/n8n:/home/node/.n8n \
    n8nio/n8n:latest
  ```
- nginx (reverse-proxy + WebSocket):
  ```bash
  sudo tee /etc/nginx/sites-available/n8n.conf >/dev/null <<'NG'
  server {
      listen 80;
      server_name n8n.your.domain.com;

      location / {
          proxy_pass http://127.0.0.1:5678;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;

          proxy_set_header Upgrade $http_upgrade;
          proxy_set_header Connection "upgrade";
          client_max_body_size 20m;
      }
  }
  NG
  sudo ln -s /etc/nginx/sites-available/n8n.conf /etc/nginx/sites-enabled/
  sudo nginx -t && sudo systemctl reload nginx
  ```
- TLS (Let's Encrypt):
  ```bash
  sudo apt-get install -y certbot python3-certbot-nginx
  sudo certbot --nginx -d n8n.your.domain.com
  ```
- Импорт workflow и URL:
  - Зайдите в n8n UI, импортируйте JSON из репозитория.
  - Активируйте workflow и возьмите Production URL.
  - В `.env` бота укажите: `N8N_WEBHOOK_URL=<Production URL>`.

## Примечания
- Если ранее использовался пример с `N8N_PORT=5678`, за nginx/SSL используйте `N8N_PORT=443` и обязательно задайте `WEBHOOK_URL`.
- Чтобы скрыть порт n8n наружу, публикуем его на `127.0.0.1`.
- Защитите UI базовой авторизацией (`N8N_BASIC_AUTH_*`).
