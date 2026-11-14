FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 libsodium23 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp يحتاجه لفك حماية يوتيوب
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py cookies.tx[t] ./

CMD ["python", "-u", "bot.py"]
