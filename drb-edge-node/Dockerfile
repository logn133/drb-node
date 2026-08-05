FROM python:3.14-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libpulse0 \
    pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install uv && uv pip install --system --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tests/ ./tests/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--reload"]
