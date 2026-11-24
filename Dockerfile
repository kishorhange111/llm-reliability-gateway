# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[anthropic]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status != 200)"
CMD ["uvicorn", "gateway.serve:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
