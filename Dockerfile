FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/inventory.db \
    LOG_PATH=/data/bot.log

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App modules (must include every imported .py or cloud shows "No module named …")
# reservation_janitor.py: run_cloud.main. tg_payments.py: bot + vendor_stores + spbc_notify.
COPY bot.py config.py db.py permissions.py payment_templates.py setup_wizard.py reports.py run_cloud.py collab.py franchise.py inventory_import.py backup.py token_pool.py spbc_notify.py site_sync.py webpanel.py order_router.py vendor_stores.py autobiller.py orders_admin.py vendor_links.py payables.py reservation_janitor.py tg_payments.py ./

# Optional docs
COPY README.md HOW_TO_USE.md SITE-LINKING.txt .env.example ./

# Render web healthcheck + Telegram long-poll
CMD ["python", "run_cloud.py"]
