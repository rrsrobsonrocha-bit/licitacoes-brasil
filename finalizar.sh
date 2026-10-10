#!/usr/bin/env bash
# Empacota os arquivos tocados, roda os testes novos e cria o commit (executar na raiz do repositório).
set -euo pipefail
PY="${PYTHON:-python}"
FILES=(
  server/config.py
  server/security.py
  server/services/email_service.py
  server/schemas.py
  server/templates/base.html
  server/templates/login.html
  server/templates/register.html
  server/templates/recuperar_senha.html
  server/templates/redefinir_senha.html
  .env.example
  server/search_engine.py
  server/templates/search.html
  server/templates/index.html
  server/templates/alerts.html
  server/services/pncp_compras.py
  server/templates/tender_detail.html
  server/models.py
  server/migrate.py
  server/main.py
  tests/test_melhorias_prioritarias.py
)
rm -f licitacoes_melhorias.zip
"$PY" -c "import sys,zipfile; z=zipfile.ZipFile('licitacoes_melhorias.zip','w',zipfile.ZIP_DEFLATED); [z.write(f) for f in sys.argv[1:]]; z.close(); print('licitacoes_melhorias.zip:', len(sys.argv)-1, 'arquivos')" "${FILES[@]}"
"$PY" -m pytest tests/test_melhorias_prioritarias.py -q
git add "${FILES[@]}"
git commit -m "feat(core): login com recuperacao de senha, filtros material/servico/outros com 27 UFs, itens e anexos internos do PNCP" \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_01Hn2Qu5FK4JRZf3xeVSZR3G"
