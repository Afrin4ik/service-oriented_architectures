FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m scripts.generate \
    && useradd --create-home --uid 10001 marketplace \
    && chown -R marketplace:marketplace /app
USER marketplace
EXPOSE 8000
CMD ["sh", "-c", "python -m app.seed && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
