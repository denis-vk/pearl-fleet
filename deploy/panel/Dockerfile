# Pearl Fleet Panel -- production image.
# Python 3.12 (the miner/agent target), no GPU, no build tools needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PANEL_DB=/data/panel.db \
    PANEL_BUNDLES_DIR=/data/bundles

WORKDIR /app
COPY panel/requirements.txt /app/panel/requirements.txt
RUN pip install --no-cache-dir -r /app/panel/requirements.txt

COPY panel /app/panel
COPY docs /app/docs

RUN mkdir -p /data/bundles
VOLUME ["/data"]
EXPOSE 8000

# single worker: SQLite + in-process state; scale with more rigs, not workers
CMD ["uvicorn", "panel.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
