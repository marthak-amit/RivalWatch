FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src src
COPY scripts scripts
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
# Headless Chromium for pages built by JavaScript (crawler/browser.py). INSTALL_BROWSER=0 builds a smaller image;
# the crawler then reports those pages as "needs browser" instead of rendering them.
ARG INSTALL_BROWSER=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONUNBUFFERED=1
RUN useradd --system --uid 1001 --no-create-home app
COPY --from=build /install /usr/local
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
      python -m playwright install --with-deps chromium && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/*; \
    fi
WORKDIR /srv
COPY alembic.ini .
COPY src src
COPY scripts scripts
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "competitor_moves.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
