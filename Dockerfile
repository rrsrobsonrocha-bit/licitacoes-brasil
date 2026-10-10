# Dockerfile for Licitações Brasil
# Multi-stage production build

FROM node:22-alpine AS nextjs-builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build || true

FROM python:3.12-slim AS runner
WORKDIR /app

# Instalar dependências essenciais de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependências Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copiar arquivos da aplicação
COPY . .

# Criar diretórios de armazenamento e logs com permissões adequadas
RUN mkdir -p /app/storage/documents /app/logs && chmod -R 755 /app/storage /app/logs

# Usuário sem privilégios de root para segurança máxima
RUN useradd -u 1001 -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENV PORT=8000 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:${PORT}/healthz || exit 1

# Render injeta $PORT em tempo de execucao; usa 8000 como padrao local
CMD ["sh", "-c", "python3 -m uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
