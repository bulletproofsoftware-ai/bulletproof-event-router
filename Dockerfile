FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

# Run as a non-root user (CIS-DI-0001 / opengrep missing-user).
# Pre-create the writable mount points and hand them to the unprivileged user so the
# app can create the SQLite DLQ and data files without root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /events /app/data \
    && chown -R appuser:appuser /events /app/data /app
USER appuser

EXPOSE 8085

ENV EVENTS_DIR=/events \
    DLQ_PATH=/events/dead-letter-queue.sqlite

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8085/health').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085", "--workers", "1"]
