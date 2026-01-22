FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-distutils \
        python3-pip \
        ffmpeg \
        libsndfile1 \
        libgomp1 \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir -r /app/requirements.txt && \
    python3 -m pip uninstall -y onnxruntime && \
    arch="$(dpkg --print-architecture)" && \
    if [ "${arch}" = "amd64" ]; then \
      python3 -m pip install --no-cache-dir onnxruntime-gpu; \
    else \
      python3 -m pip install --no-cache-dir onnxruntime; \
    fi

RUN python3 -c "import nltk; nltk.download('averaged_perceptron_tagger_eng')"
ENV NLTK_DATA=/root/nltk_data

COPY src /app/src

CMD ["python3", "-m", "uvicorn", "src.backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "debug", "--access-log"]
