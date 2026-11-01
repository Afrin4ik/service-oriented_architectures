FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m scripts.generate
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
CMD ["uvicorn", "booking.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
