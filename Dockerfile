FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY run.py ./

RUN mkdir -p /app/data /app/secrets

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"

CMD ["python", "run.py"]
