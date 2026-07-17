FROM node:20-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:20-alpine AS e2e-frontend
WORKDIR /app
COPY frontend/e2e-server.mjs ./server.mjs
COPY --from=frontend-build /build/frontend/dist ./dist
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD wget -q -O - http://127.0.0.1:4173/health >/dev/null || exit 1
EXPOSE 4173
CMD ["node", "server.mjs"]

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY --from=frontend-build /build/frontend/dist ./frontend/dist
RUN pip install --no-cache-dir .
RUN addgroup --system incidentlab && adduser --system --ingroup incidentlab incidentlab \
    && mkdir -p /app/data && chown -R incidentlab:incidentlab /app
USER incidentlab
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "incident_lab.api:app", "--host", "0.0.0.0", "--port", "8000"]
