FROM python:3.12-slim

WORKDIR /app

# Persist SQLite + logs outside the image layer when a volume is mounted at /data
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/inventory.db \
    LOG_PATH=/data/bot.log

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py db.py permissions.py payment_templates.py setup_wizard.py ./

# Optional docs (not required at runtime)
COPY README.md HOW_TO_USE.md .env.example ./

CMD ["python", "bot.py"]
