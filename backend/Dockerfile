# Two images from one file:
#   app    (api, scheduler, migrate): no browser
#   worker (crawls): adds Playwright and Chromium's headless shell, for pages built by JavaScript
# The browser, then our dependencies, then the code: a code change only re-copies src/, a dependency change never
# re-downloads the browser.

FROM python:3.12-slim AS deps
WORKDIR /build
COPY pyproject.toml ./
# dependencies only: a stub package satisfies the build, then the stub is dropped
RUN mkdir -p src/competitor_moves && touch src/competitor_moves/__init__.py \
 && pip install --no-cache-dir --prefix=/install . \
 && rm -rf /install/lib/python3.12/site-packages/competitor_moves*

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv/src PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN useradd --system --uid 1001 --no-create-home app
WORKDIR /srv

# Chromium's headless shell (no full Chromium, no ffmpeg) plus the system libraries it needs. This layer depends only on
# the Playwright version, so changing our dependencies or code never downloads the browser again.
FROM base AS browser-base
RUN pip install --no-cache-dir playwright==1.63.0 `# same version as the browser extra in pyproject.toml` \
 && python -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache

FROM base AS app
COPY --from=deps /install /usr/local
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "competitor_moves.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM browser-base AS worker
COPY --from=deps /install /usr/local
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
CMD ["python", "-m", "competitor_moves.workers.worker"]
