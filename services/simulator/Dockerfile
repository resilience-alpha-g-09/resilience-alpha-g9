FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY main.py .
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser
EXPOSE 9100
CMD ["python", "main.py"]
