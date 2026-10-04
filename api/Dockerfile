FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.source=https://github.com/psewdon1m/preza

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 preza && useradd --uid 10001 --gid preza --no-create-home preza \
    && mkdir /data && chown preza:preza /data
COPY api/app ./app
COPY web ./web
ENV WEB_DIR=/app/web
ARG PREZA_VERSION=development
ENV PREZA_VERSION=$PREZA_VERSION
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM runtime AS test
USER root
COPY tests/requirements.txt /tmp/test-requirements.txt
RUN pip install --no-cache-dir -r /tmp/test-requirements.txt
COPY tests /app/tests
COPY scripts /app/scripts
COPY sample.pdf /app/sample.pdf
COPY compose.production.yml /app/compose.production.yml
COPY nginx/preza.conf /app/nginx/preza.conf
CMD ["python", "-m", "pytest", "-q", "tests"]

