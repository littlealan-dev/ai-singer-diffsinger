FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libgomp1 \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && \
    pip uninstall -y onnxruntime && \
    pip install --no-cache-dir onnxruntime-gpu

RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng')"
ENV NLTK_DATA=/root/nltk_data

COPY src /app/src

CMD ["uvicorn", "src.backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "debug", "--access-log"]
