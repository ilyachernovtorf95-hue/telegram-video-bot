FROM node:22-bookworm-slim AS pot-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
WORKDIR /opt/bgutil-ytdlp-pot-provider/server
RUN npm ci && npx tsc

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pot-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-builder /opt/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
