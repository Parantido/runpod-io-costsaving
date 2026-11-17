FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml ./
COPY runpod_ops ./runpod_ops
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /usr/local/bin/runpodctl \
       https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-amd64 \
    && chmod +x /usr/local/bin/runpodctl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

RUN useradd --create-home --uid 10001 runpod \
    && mkdir -p /data && chown runpod:runpod /data

USER runpod
WORKDIR /app
ENV RUNPOD_OPS_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

CMD ["runpod-ops", "serve"]
