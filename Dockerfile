FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/inventory.db \
    LOG_PATH=/data/bot.log

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App modules (must include every imported .py or cloud shows "No module named …")
COPY bot.py config.py db.py permissions.py payment_templates.py setup_wizard.py reports.py run_cloud.py collab.py franchise.py inventory_import.py backup.py token_pool.py ./

# Optional docs
COPY README.md HOW_TO_USE.md .env.example ./

# Render web healthcheck + Telegram long-poll
CMD ["python", "run_cloud.py"]
