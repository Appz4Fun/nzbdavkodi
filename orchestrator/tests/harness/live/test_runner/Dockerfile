FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tests
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY live_common.py test_live.py test_live_extreme.py ./
CMD ["pytest", "/tests/test_live.py", "-v", "--tb=short", "--no-header"]
