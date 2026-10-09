# Two images from one file:
#   app    (api, scheduler, migrate): no browser
#   worker (crawls): adds Playwright and Chromium's headless shell, for pages built by JavaScript
# Dependencies and the browser sit in layers before the code, so a code change only re-copies src/.

FROM python:3.12-slim AS deps
WORKDIR /build
COPY pyproject.toml ./
# dependencies only: a stub package satisfies the build, then the stub is dropped
RUN mkdir -p src/competitor_moves && touch src/competitor_moves/__init__.py \
 && pip install --no-cache-dir --prefix=/install . \
 && rm -rf /install/lib/python3.12/site-packages/competitor_moves*

FROM python:3.12-slim AS browser-deps
RUN pip install --no-cache-dir --prefix=/install playwright==1.63.0

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv/src PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN useradd --system --uid 1001 --no-create-home app
COPY --from=deps /install /usr/local
WORKDIR /srv

FROM runtime AS browser-runtime
COPY --from=browser-deps /install /usr/local
# headless shell only (no full Chromium, no ffmpeg) plus the system libraries it needs
RUN python -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache

FROM runtime AS app
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "competitor_moves.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM browser-runtime AS worker
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
CMD ["python", "-m", "competitor_moves.workers.worker"]
