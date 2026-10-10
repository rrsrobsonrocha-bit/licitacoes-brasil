#!/usr/bin/env bash
# Empacota os arquivos tocados, roda a suíte nova e cria o commit (executar na raiz do repositório).
set -euo pipefail
PY="${PYTHON:-python}"
FILES=(
  server/services/pdf_generator.py server/main.py server/templates/juridico.html
  server/models.py server/migrate.py server/services/document_vault.py
  server/templates/documentos.html server/templates/base.html .gitignore
  server/services/kit_builder.py server/templates/tender_detail.html
  server/templates/partials_deal_card.html server/services/portal_collector.py
  server/templates/index.html tests/test_fusion_features.py
)
rm -f licitacoes_patch.zip
"$PY" -c "import sys,zipfile; z=zipfile.ZipFile('licitacoes_patch.zip','w',zipfile.ZIP_DEFLATED); [z.write(f) for f in sys.argv[1:]]; z.close(); print('licitacoes_patch.zip:', len(sys.argv)-1, 'arquivos')" "${FILES[@]}"
"$PY" -m pytest tests/test_fusion_features.py -q
git add "${FILES[@]}"
git commit -m "feat(core): automacao juridica pdf, cofre com alertas, kit zip e filtro simplificado" \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_01Hn2Qu5FK4JRZf3xeVSZR3G"
