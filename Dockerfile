FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    LOCALREACH_DB=/data/localreach.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db

RUN useradd -m -u 10001 localreach && mkdir -p /data && chown -R localreach /data
USER localreach

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
