FROM python:3.12-slim-bookworm

RUN apt-get update -q && \
    apt-get install -y -q --no-install-recommends \
        ffmpeg pulseaudio gcc g++ && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md squeezy.py ./
COPY tests/ tests/
RUN pip install --no-cache-dir -e ".[test]"

# PulseAudio null sink for headless audio
ENV PULSE_SINK=test_sink

CMD ["squeezy", "-v"]
