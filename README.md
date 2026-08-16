# Telegram Video Bot

Telegram-бот принимает ссылку на видео, скачивает ролик через `yt-dlp` и отправляет файл обратно в тот же чат.

## Что нужно

- Python 3.10+
- `ffmpeg`
- токен Telegram-бота от BotFather
- переменная окружения `TELEGRAM_BOT_TOKEN`

## Локальный запуск / Codespaces

```bash
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='ВАШ_ТОКЕН'
python bot.py
```

## Docker

```bash
docker build -t telegram-video-bot .
docker run --rm -e TELEGRAM_BOT_TOKEN='ВАШ_ТОКЕН' telegram-video-bot
```

## Важно

- Не добавляйте токен бота в GitHub.
- Некоторые сайты требуют авторизацию/cookies или блокируют загрузку.
- Используйте бота только для контента, который вам разрешено скачивать и пересылать.
- Обычный Telegram Bot API отправляет video-файлы размером до 50 МБ.
