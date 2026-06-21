FROM python:3.12-slim

WORKDIR /app

# Install system deps for libtorrent
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY tests/integration/requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt

# Copy test server code
COPY tests/integration/server/ ./server/

EXPOSE 8080

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8080"]
