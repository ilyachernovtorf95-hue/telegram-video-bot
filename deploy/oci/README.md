# Перенос Video Downloader на Oracle Cloud Always Free

Этот способ сохраняет текущий код бота: Telegram long polling, скачивание и
разбиение видео, проверку H.264 для iPhone, Gemini-анализ и локальный резерв.
Контейнер запускается на ARM-сервере Oracle. Публичный HTTP-порт боту не нужен.

## Условия до запуска

1. Нужен собственный аккаунт Oracle Cloud с доступным Always Free сервером
   `VM.Standard.A1.Flex` в **домашнем регионе** аккаунта. У бесплатного аккаунта
   общий лимит 2 OCPU и 12 ГБ RAM. Выбирай только ресурсы с пометкой
   **Always Free Eligible**. Не выбирай платный тариф или Pay As You Go.
2. Oracle обычно проверяет номер телефона и карту при регистрации. При
   проверке карты возможна временная авторизация суммы; бесплатные ресурсы
   не предполагают подписки без отдельного перехода на платный аккаунт.
   Возможность регистрации и сервиса зависит от страны и правил Oracle;
   указывай только достоверные данные.
3. Выдели один ARM-сервер Ubuntu 24.04 LTS, 2 OCPU, 12 ГБ RAM и загрузочный
   диск 100 ГБ. 50 ГБ также подходит для коротких видео, но временные файлы
   длинных роликов могут занять много места. Общий бесплатный лимит хранилища
   включает загрузочный диск.
4. Открой для SSH только порт 22 со своего IP. Порты 80/443 не нужны: бот сам
   подключается к Telegram API. Сохрани приватный SSH-ключ только у себя.

Источник лимитов: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

## На сервере

Подключись по SSH как `ubuntu`. Установи Docker Engine и Compose plugin по
официальной инструкции для Ubuntu/arm64:
https://docs.docker.com/engine/install/ubuntu/

Проверь Docker:

```sh
sudo docker compose version
```

Склонируй ветку подготовки Oracle и создай закрытый файл переменных:

```sh
git clone --branch deployment/oci-free --single-branch https://github.com/ilyachernovtorf95-hue/telegram-video-bot.git
cd telegram-video-bot
cp .env.example .env
chmod 600 .env
nano .env
```

В `.env` заполни `TELEGRAM_BOT_TOKEN` и `GEMINI_API_KEY`. Перенеси остальные
использовавшиеся в Railway переменные (например, `VK_ACCESS_TOKEN` и cookies)
только если они были настроены. Не отправляй `.env`, ключи или токены в чат,
GitHub и скриншоты. Для многострочных YouTube cookies используй
`YOUTUBE_COOKIES_B64`, а не `YOUTUBE_COOKIES`.

До запуска нового poller убедись, что Railway больше не выполняет этого бота.
Два активных экземпляра с одним Telegram-токеном вызовут HTTP 409 Conflict.
Текущий статус `Trial expired` сам по себе не является долговременной защитой
от повторного включения Railway.

Проверка настроек и запуск:

```sh
sudo sh deploy/oci/preflight.sh
sudo docker compose up -d --build
sudo docker compose logs --tail=100 bot
```

В логах должны быть `PRIMARY: starting responsive Telegram bot` и
`Responsive Telegram runner started`. После этого проверь `/start`, короткую
ссылку, видео на iPhone, AI-разбор, полную транскрипцию и `.md`. Длинное видео
проверяй отдельно: диск и скорость загрузки зависят от исходника.

Обслуживание:

```sh
sudo docker compose ps
sudo docker compose logs --tail=100 bot
git pull --ff-only origin deployment/oci-free
sudo docker compose up -d --build
```

Последняя пара команд обновляет код из этой ветки и пересобирает контейнер.
Не запускай `docker compose down -v`: удаление томов/данных здесь не требуется.

Ограничения: Oracle может не выдать ARM-сервер из-за отсутствия свободной
ёмкости в домашнем регионе и может отзывать простаивающие бесплатные машины.
Перенос хостинга не устраняет блокировки YouTube для IP дата-центра и лимиты
бесплатного Gemini API; существующие резервные пути бота остаются включены.
