FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Service state (SQLite) lives outside the image — mount a volume here.
ENV WEBMAIL_DB=/data/webmail_aliases.db
VOLUME ["/data"]

EXPOSE 18400
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18400/health')"

CMD ["python", "-m", "uvicorn", "rupochta_server:app", "--host", "0.0.0.0", "--port", "18400"]
