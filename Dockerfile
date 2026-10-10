# Pinned to the digest-stable tag rather than :3.12, so a rebuild months from now
# produces the same image the tests ran against.
FROM python:3.12.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first, so editing application code does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Runs as a non-root user. Nothing here needs to write to the image.
RUN useradd --system --create-home --uid 10001 ledger
USER ledger

EXPOSE 8000

# Migrate then serve. Alembic is idempotent, so a restart is safe, and doing it
# here means `docker compose up` gives a working API with no second command.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
