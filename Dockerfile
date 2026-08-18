FROM node:22-bookworm-slim AS pot-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
WORKDIR /opt/bgutil-ytdlp-pot-provider/server
RUN npm ci && npx tsc

FROM debian:bookworm-slim AS native-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git \
    && cmake -S whisper.cpp -B whisper.cpp/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    && cmake --build whisper.cpp/build -j2 --target whisper-cli

RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git \
    && cmake -S llama.cpp -B llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    && cmake --build llama.cpp/build -j2 --target llama-cli

RUN mkdir -p /models \
    && curl -L --fail --retry 4 --retry-delay 3 \
      -o /models/ggml-small-q5_1.bin \
      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin?download=true \
    && curl -L --fail --retry 4 --retry-delay 3 \
      -o /models/Qwen3-0.6B-Q8_0.gguf \
      https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q8_0.gguf?download=true

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates chromium libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pot-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-builder /opt/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider
COPY --from=native-builder /src/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=native-builder /src/llama.cpp/build/bin/llama-cli /usr/local/bin/llama-cli
COPY --from=native-builder /models /opt/models

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py local_ai.py ./

ENV PYTHONUNBUFFERED=1
ENV CHROME_PATH=/usr/bin/chromium
ENV WHISPER_CPP_MODEL=/opt/models/ggml-small-q5_1.bin
ENV LOCAL_LLM_MODEL=/opt/models/Qwen3-0.6B-Q8_0.gguf
ENV LOCAL_AI_THREADS=2

CMD ["sh", "-c", "node /opt/bgutil-ytdlp-pot-provider/server/build/main.js >/tmp/pot-provider.log 2>&1 & sleep 2; exec python bot.py"]
