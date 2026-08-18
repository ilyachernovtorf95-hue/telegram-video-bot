# Telegram Video Bot

Telegram-бот принимает ссылку на видео, скачивает ролик через `yt-dlp`, отправляет его в Telegram, локально распознаёт речь через `faster-whisper`, делает бесплатную локальную выжимку и формирует `.md`-заметку для Obsidian.

OpenAI API для транскрибации и выжимки не требуется.

## Что нужно

- Python 3.12
- `ffmpeg`
- токен Telegram-бота от BotFather
- переменная окружения `TELEGRAM_BOT_TOKEN`

## Локальный Whisper

По умолчанию используется сбалансированный профиль для Railway:

- `WHISPER_MODEL=base`
- `WHISPER_COMPUTE_TYPE=int8`
- `WHISPER_BEAM_SIZE=5`
- `WHISPER_LANGUAGE=auto`
- `WHISPER_CPU_THREADS=2`
- VAD включён
- добавлен contextual prompt для корректного распознавания терминов `ChatGPT`, `OpenAI`, `Obsidian`, `Whisper` и т. п.
- после распознавания применяется консервативная очистка повторов и типичных ASR-искажений

Настройки можно переопределить через Railway Variables. На Trial Railway доступен ограниченный объём RAM, поэтому модели `small`, `medium` и `large` без увеличения ресурсов использовать не рекомендуется.

Для преимущественно русских видео можно задать `WHISPER_LANGUAGE=ru` — это обычно повышает стабильность на коротких роликах. Для смешанного контента оставьте `auto`.

## Локальный запуск

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

- Не добавляйте токен бота, cookies и другие секреты в GitHub.
- Некоторые сайты требуют авторизацию/cookies или блокируют загрузку отдельных публикаций.
- Используйте бота только для контента, который вам разрешено скачивать и пересылать.
- Обычный Telegram Bot API ограничивает размер отправляемых video-файлов, поэтому большие ролики бот автоматически делит на части без перекодирования.
