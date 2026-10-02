# Wassup app: pipeline, API and the built UI in one image.
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pipeline/pyproject.toml pipeline/pyproject.toml
COPY pipeline/wassup pipeline/wassup
RUN pip install -e ./pipeline
COPY db db
COPY config config
COPY --from=ui /ui/dist ui/dist
EXPOSE 8000
CMD ["wassup", "dev", "--host", "0.0.0.0", "--port", "8000"]
