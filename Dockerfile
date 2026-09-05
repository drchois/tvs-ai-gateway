FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=prod

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY configs ./configs
RUN pip install --no-cache-dir .

RUN addgroup --system gateway && adduser --system --ingroup gateway gateway \
    && mkdir -p /app/data && chown -R gateway:gateway /app
USER gateway

EXPOSE 7072
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7072"]
