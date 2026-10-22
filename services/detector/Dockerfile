# Contexte de build = racine du repo (pour partager ml/features.py)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY services/detector/requirements.txt .
RUN pip install -r requirements.txt
COPY ml/features.py ./features.py
COPY services/detector/main.py ./main.py
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
