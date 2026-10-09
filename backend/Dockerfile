# Three images from one file:
#   app     (api, scheduler, migrate): no browser
#   worker  (crawls): app + the Playwright client only; it drives the browser over the network (BROWSER_WS_URL)
#   browser (the heavy one: Chromium's headless shell): a Playwright browser server for pages built by JavaScript. It can
#           run on another machine, e.g. your own computer, and the worker reaches it by IP:
#           ws://<that computer's IP>:3000/<BROWSER_SERVER_SECRET>
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

# Playwright's client and server versions must match: keep these in step with the browser extra in pyproject.toml.
FROM base AS playwright
RUN pip install --no-cache-dir playwright==1.63.0

# Chromium's headless shell (no full Chromium, no ffmpeg) plus the system libraries it needs. Depends only on the
# Playwright version, so changing our dependencies or code never downloads the browser again.
FROM playwright AS browser
RUN python -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache
USER app
EXPOSE 3000
# the secret is the URL path, the server's only key: refuse to start without a long one
CMD ["sh", "-c", "[ ${#BROWSER_SERVER_SECRET} -ge 16 ] || { echo 'set BROWSER_SERVER_SECRET (16+ characters)' >&2; exit 1; }; exec python -m playwright run-server --host 0.0.0.0 --port 3000 --path \"/$BROWSER_SERVER_SECRET\" --max-clients 2"]

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

FROM playwright AS worker
COPY --from=deps /install /usr/local
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
CMD ["python", "-m", "competitor_moves.workers.worker"]
