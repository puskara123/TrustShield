FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Only install server deps — NOT the full ML training stack
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy source
COPY . .

# Install the trustshield package itself (no deps, already installed above)
RUN pip install --no-deps -e .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 7860

CMD ["uvicorn", "trustshield.server:create_app", "--host", "0.0.0.0", "--port", "7860", "--factory"]