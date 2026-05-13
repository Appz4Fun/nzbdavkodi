FROM python:3.12-slim

# ffmpeg generates the sample fixture at image-build time.
# Baking it in means the image is self-contained; no bind-mount needed.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# 1-second 160×120 test-pattern MKV, ~50 KB, sufficient for byte-sample
# validation (offsets 0, 100, and 4096 all fall within the file).
RUN mkdir -p /fixtures && \
    ffmpeg -f lavfi -i testsrc=duration=1:size=160x120:rate=1 \
           -f lavfi -i sine=frequency=440:duration=1 \
           -c:v libx264 -c:a aac -shortest -y /fixtures/sample.mkv \
           2>/dev/null

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
