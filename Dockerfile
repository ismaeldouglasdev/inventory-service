# =============================================================================
# Stage 1 — Builder / Dependency Install
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /install

COPY pyproject.toml .

RUN pip install --no-cache-dir --prefix=/install \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.30.0" \
    "pydantic>=2.0" \
    "pydantic-settings>=2.0" \
    "sqlalchemy[asyncio]>=2.0" \
    "aiosqlite>=0.20.0" \
    "alembic>=1.13.0" \
    "httpx>=0.27.0" \
    "python-dotenv>=1.0" \
    "pycryptodome>=3.20.0"

# =============================================================================
# Stage 2 — Runtime
# =============================================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 app

# Copy application code
COPY alembic/ alembic/
COPY alembic.ini .
COPY pyproject.toml .
COPY app/ app/

# Runtime data directory
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
