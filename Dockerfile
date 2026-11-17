FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HANDOFF_DB=/data/history.sqlite
WORKDIR /app
COPY pyproject.toml .
COPY handoff ./handoff
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home bridge \
    && install -d -o bridge -g bridge -m 0700 /data
USER bridge
EXPOSE 8000
CMD ["handoff", "serve", "--transport", "http", "--host", "0.0.0.0"]
