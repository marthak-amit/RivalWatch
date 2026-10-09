FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN useradd --system --uid 1001 --no-create-home app
COPY --from=build /install /usr/local
WORKDIR /srv
COPY alembic.ini .
COPY src src
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "competitor_moves.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
