# Simple Presentation Site

Сайт для публикации одной PDF-презентации как набора PNG-слайдов:

- `/view` — публичный просмотр
- `/admin` — загрузка и удаление презентации

## Что делает система

1. В админке загружается PDF.
2. Backend конвертирует страницы PDF в PNG (`pdftoppm` / poppler).
3. PNG сохраняются в Docker volume.
4. Страница `/view` показывает слайды один под другим.

## Запуск (Docker Compose)

1. Скопируйте `.env.example` в `.env`.
2. Заполните:
   - `SITE_DOMAIN` (ваш домен)
   - `LETSENCRYPT_EMAIL`
3. Убедитесь, что DNS домена указывает на сервер.
4. Убедитесь, что порты `80` и `443` доступны серверу (для автосертификатов).
5. Запустите:

```bash
docker compose up -d --build
```

## Боевое развертывание (рекомендуемый путь)

Подготовлены скрипты для сервера:

- `scripts/check-ports.sh` — проверка конфликтов портов
- `scripts/deploy.sh` — сборка и запуск контейнеров
- `scripts/issue-cert.sh` — выпуск SSL-сертификата Let's Encrypt
- `scripts/renew-cert.sh` — продление сертификата

Шаги:

```bash
cp .env.production .env.production.local   # опционально, если хотите хранить свои значения отдельно
nano .env.production                        # проверьте email
chmod +x scripts/*.sh
./scripts/deploy.sh
./scripts/issue-cert.sh
```

## SSL (автоматически)

Используется `nginx` как reverse proxy и `certbot` для получения сертификата Let's Encrypt.

Важно:

- Для реального SSL нужен публичный домен.
- DNS `A` запись домена должна указывать на сервер (для вашего случая: `loki-panel.shmoza.net -> 199.68.196.107`).
- На сервере должны быть доступны `80/443`.
- После первого запуска контейнер `nginx` стартует с временным self-signed сертификатом (чтобы сервис поднялся).
- Затем получите боевой сертификат командой:

Альтернатива без скриптов:

```bash
docker compose --env-file .env.production up -d --build
docker compose --env-file .env.production run --rm certbot certonly --webroot -w /var/www/certbot -d loki-panel.shmoza.net --email you@example.com --agree-tos --no-eff-email
docker compose --env-file .env.production exec proxy nginx -s reload
```

- Для продления сертификата (например, по cron на сервере):

```bash
./scripts/renew-cert.sh
```

## Ограничения текущей версии

- Одновременно хранится только одна презентация.
- Новую презентацию можно загрузить только после удаления текущей.
- DPI рендера задается через `PDF_RENDER_DPI` (по умолчанию `300`).

## Примечание про "оригинальное разрешение"

У PDF обычно нет фиксированного "пиксельного" разрешения как у фотографии. Итоговый размер PNG зависит от DPI рендера.
Сейчас это контролируется переменной `PDF_RENDER_DPI`.
