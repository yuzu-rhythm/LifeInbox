FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install from the existing pyproject dependency declaration.  Only the
# package source and metadata needed to build the application are copied.
COPY pyproject.toml README.md ./
COPY src/life_inbox ./src/life_inbox

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["python", "-m", "life_inbox.cloud_run"]
