FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.12-slim

# SEC-011: run as a dedicated, unprivileged user rather than root.
RUN groupadd --system rupochta && \
    useradd --system --gid rupochta --home-dir /home/rupochta --create-home rupochta

WORKDIR /app

COPY --from=builder /root/.local /home/rupochta/.local
COPY . .

# Service state (SQLite) lives outside the image — mount a volume here.
ENV WEBMAIL_DB=/data/webmail_aliases.db
ENV PATH=/home/rupochta/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown -R rupochta:rupochta /app /data /home/rupochta

VOLUME ["/data"]

USER rupochta

EXPOSE 18400
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18400/health')"

CMD ["python", "-m", "uvicorn", "rupochta_server:app", "--host", "0.0.0.0", "--port", "18400"]
