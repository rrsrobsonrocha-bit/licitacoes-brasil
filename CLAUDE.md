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
- `server/integrations/opportunity_collector.py`, `server/services/portal_collector.py`: coleta na API oficial do PNCP.
- `server/services/auto_sync.py`: auto-warmup dos últimos 15 dias e atualização periódica do catálogo (sem clique do usuário).
- `server/templates/partials_tender_drawer.html` + `GET /api/tenders/{id}/drawer`: drawer lateral de detalhes
  (busca e painel) com itens e anexos oficiais do PNCP e realce dos termos pesquisados.
- `server/services/minutas.py` + `/api/minutas`: declarações e proposta readequada (Lei 14.133/2021) em DOCX.
- `server/routes/webhooks.py`: webhook da Hotmart (modo de espera: registra eventos, não altera contas).
- `server/services/dispute_monitor.py`: Sala de Disputa (lotes e robô de lances, por usuário).
- `server/templates/`: páginas (busca, painel, funil, agenda, disputa, jurídico).

## Regras do produto

- **Ambiente de produção, sem dados fictícios.** Nenhum certame, conta, negócio, evento, lote de
  disputa ou concorrente pode ser inventado. Sem resposta da API oficial, a coleta não insere nada.
  Dados de exemplo existem apenas na suíte de testes (`seed_dados_de_teste`, `tests/pncp_simulado.py`).
- **Catálogo global, acompanhamento privado.** Editais sincronizados são compartilhados por todas as
  contas. Negócios (funil), Favoritos, Agenda, Alertas, logs de webhooks e lotes da Sala de Disputa
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
- **Controle de acesso.** O middleware `controle_de_acesso_middleware` (server/main.py) derruba na hora
  a sessão de conta bloqueada e, com `ACESSO_EXIGE_LOGIN`, exige login fora da vitrine (`ROTAS_PUBLICAS`).
  O menu "Gestão" e `/admin`/`/gestao` são exclusivos de ADMIN.

## Desenvolvimento

- Testes: `.venv/Scripts/python.exe -m pytest -q` (banco SQLite temporário, rede externa bloqueada).
- Rodar localmente: `python -m uvicorn server.main:app --reload`.
