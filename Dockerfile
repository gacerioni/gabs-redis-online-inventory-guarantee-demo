# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy app code (expect app_rdi.py at repo root)
COPY app_rdi.py ./

# Runtime env (override at run-time if needed)
ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["uvicorn", "app_rdi:app", "--host", "0.0.0.0", "--port", "8000"]
