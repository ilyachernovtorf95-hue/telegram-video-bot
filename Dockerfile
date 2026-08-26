FROM node:22-bookworm-slim AS pot-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch 1.3.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
WORKDIR /opt/bgutil-ytdlp-pot-provider/server
RUN npm ci && npx tsc

FROM debian:bookworm-slim AS whisper-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch v1.9.1 https://github.com/ggml-org/whisper.cpp.git \
    && cmake -S whisper.cpp -B whisper.cpp/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    && cmake --build whisper.cpp/build -j2 --target whisper-cli

RUN mkdir -p /models \
    && curl -L --fail --retry 4 --retry-delay 3 \
      -o /models/ggml-tiny.bin \
      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin?download=true

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates chromium xvfb xauth libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pot-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-builder /opt/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider
COPY --from=whisper-builder /src/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-builder /models /opt/models

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py local_ai.py gemini_ai.py sitecustomize.py ./
COPY yt_dlp_plugins ./yt_dlp_plugins

ENV PYTHONUNBUFFERED=1
ENV CHROME_PATH=/usr/bin/chromium
ENV WHISPER_CPP_MODEL=/opt/models/ggml-tiny.bin
ENV WHISPER_LANGUAGE=ru
ENV LOCAL_AI_THREADS=1
ENV GEMINI_MODELS=gemini-3.7-flash,gemini-3.5-flash-lite
ENV GEMINI_FILE_TIMEOUT=180
ENV GEMINI_REQUEST_TIMEOUT=180
ENV GEMINI_UPLOAD_TIMEOUT=300

# Only the primary Railway project is allowed to long-poll Telegram.
# Railway-provided project IDs are immutable and therefore safer than project names.
CMD ["sh", "-c", "if [ -n \"${RAILWAY_PROJECT_ID:-}\" ] && [ \"$RAILWAY_PROJECT_ID\" != \"0782ee62-74b0-447a-94e3-e88cd24c2e01\" ]; then echo \"Standby deployment: Telegram polling disabled for Railway project ${RAILWAY_PROJECT_NAME:-unknown} ($RAILWAY_PROJECT_ID)\"; exec tail -f /dev/null; fi; node /opt/bgutil-ytdlp-pot-provider/server/build/main.js >/tmp/pot-provider.log 2>&1 & sleep 2; exec xvfb-run -a -s '-screen 0 1280x720x24' python bot.py"]
