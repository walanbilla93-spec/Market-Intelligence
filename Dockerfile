FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir .
ENV MI_DATA_DIR=/data MI_CONFIG=/app/config.json
VOLUME ["/data"]
CMD ["orayan-market-intel", "run"]

