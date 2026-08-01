FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

ENV DEFCON_OPS_DB=/app/data/ops.db
ENV DEFCON_OPS_EVIDENCE_DIR=/app/data/evidence

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
