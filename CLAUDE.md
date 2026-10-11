# Licitações Brasil

Este projeto se chama **Licitações Brasil**, e esse é o único nome a usar em mensagens, recaps,
commits, textos de interface, docstrings e documentação. Em identificadores técnicos, use o slug
`licitacoes-brasil` (pacotes, repositório, URLs) ou `licitacoes_brasil` (nomes Python/SQL).
Commits são assinados como `Licitações-Brasil`.

## O que é

Plataforma de busca e acompanhamento de licitações públicas brasileiras (PNCP / Compras.gov.br):
FastAPI + Jinja2 + SQLAlchemy (SQLite em desenvolvimento, PostgreSQL em produção).

- `server/main.py`: rotas HTML e API.
- `server/models.py`, `server/migrate.py`: modelos e migrações idempotentes, executadas no boot.
- `server/database.py`: normaliza a `DATABASE_URL` (`postgres://` → driver psycopg instalado, remove `?schema=`),
  TLS obrigatório fora do localhost e pool compatível com o pooler do Supabase. `python -m server.migrate`
  cria o esquema completo num banco vazio.
- `server/integrations/opportunity_collector.py`, `server/services/portal_collector.py`: coleta na API oficial do PNCP.
- `server/services/auto_sync.py`: agendador híbrido do catálogo (horário de Brasília). O boot fica em repouso
  (nunca varre 15 dias); de dia, a cada 2 h, só as últimas 24 h + itens dos editais novos (o botão
  "Sincronizar agora" faz o mesmo); das 02h às 04h, consolidação dos últimos 15 dias + fila de itens ME/EPP.
  Exceção: boot com catálogo vazio (menos de `AUTO_SYNC_MIN_VIGENTES` vigentes) recupera os 15 dias na hora.
  `/healthz/db` mostra o tipo de banco e o volume do catálogo (sqlite em produção = disco efêmero).
- `server/templates/partials_tender_drawer.html` + `GET /api/tenders/{id}/drawer`: drawer lateral de detalhes
  (busca e painel) com itens e anexos oficiais do PNCP e realce dos termos pesquisados.
- `server/services/minutas.py` + `/api/minutas`: declarações e proposta readequada (Lei 14.133/2021) em DOCX.
- `server/services/certidoes.py` + `POST /api/documents/auto-import/{cnpj}`: pacote de habilitação por CNPJ no
  Cofre. Só entra válido o que foi consultado de fato (situação cadastral via BrasilAPI; CEIS/CNEP com
  `PORTAL_TRANSPARENCIA_API_KEY`); CND, CRF e CNDT entram PENDENTES com link oficial até o PDF ser anexado
  (`POST /api/documentos/{id}/anexar`). O Cofre é privado por conta (`DocumentoEmpresa.user_id`).
- `server/services/dossie.py` + `GET /api/fornecedores/dossie/{cnpj}`: Dossiê 360° (Receita/QSA, contratos do
  PNCP filtrados por `fornecedor_ni`, sanções, selo de aptidão), cache de 24 h em `dossies_fornecedor`.
- `server/routes/webhooks.py`: webhook da Hotmart (modo de espera: registra eventos, não altera contas).
- `server/services/dispute_monitor.py`: Sala de Disputa (lotes e robô de lances, por usuário).
- `server/templates/`: páginas (busca, painel, funil, agenda, disputa, jurídico).

## Regras do produto

- **Ambiente de produção, sem dados fictícios.** Nenhum certame, conta, negócio, evento, lote de
  disputa ou concorrente pode ser inventado. Sem resposta da API oficial, a coleta não insere nada.
  Dados de exemplo existem apenas na suíte de testes (`seed_dados_de_teste`, `tests/pncp_simulado.py`).
- **Catálogo global, acompanhamento privado.** Editais sincronizados são compartilhados por todas as
  contas. Negócios (funil), Favoritos, Agenda, Alertas, Cofre de Documentos, logs de webhooks e lotes da Sala de Disputa
  são isolados por usuário (`user_id` / `owner_id`).
- **Datas oficiais.** `Tender.publication_date` é a `dataPublicacaoPncp` (horário de Brasília). Filtros
  por período ("Publicadas hoje", "Últimos 7 dias", De/Até) usam essa data, nunca a data de inserção local.
- Contas iniciais apenas via ambiente: `ADMIN_EMAIL` / `ADMIN_PASSWORD` (ADMIN) e `USUARIO_INICIAL_*` (USER),
  nunca com senha no código. Contas demo antigas são apagadas no boot; registros legados sem dono
  vão para `ORPHAN_OWNER_EMAIL` (ou `ADMIN_EMAIL`, ou a conta real principal).
- **Assinaturas.** `User.motivo_bloqueio()` é a regra única de acesso (suspensa, cancelada ou prazo
  vencido bloqueia; ADMIN nunca). Gestão manual em `/api/admin/users/*`, só para ADMIN. Conta nova:
  plano TRIAL de `TRIAL_DIAS` (7) dias.
- **Filtros avançados.** Benefício ME/EPP usa `Tender.itens_exclusivos_me_epp` / `itens_ampla_concorrencia`,
  calculados dos itens oficiais (`tipoBeneficio` do PNCP); sem itens lidos o certame fica fora do filtro.
  Esfera aceita os códigos do PNCP (F/E/M/D/N) e os nomes por extenso; portal de disputa vem do domínio de `url_disputa`.
- **Sessões.** JWT de 30 dias (`REMEMBER_ME_DAYS`) em cookie `lb_session` persistente (HttpOnly, SameSite=Lax,
  Secure em produção). A chave (`AUTH_SECRET`/`SECRET_KEY`) nunca é aleatória por boot; sem ela, deriva da DATABASE_URL.
- **Controle de acesso.** O middleware `controle_de_acesso_middleware` (server/main.py) derruba na hora
  a sessão de conta bloqueada e, com `ACESSO_EXIGE_LOGIN`, exige login fora da vitrine (`ROTAS_PUBLICAS`).
  O menu "Gestão" e `/admin`/`/gestao` são exclusivos de ADMIN.

## Desenvolvimento

- Testes: `.venv/Scripts/python.exe -m pytest -q` (banco SQLite temporário, rede externa bloqueada).
- Rodar localmente: `python -m uvicorn server.main:app --reload`.
