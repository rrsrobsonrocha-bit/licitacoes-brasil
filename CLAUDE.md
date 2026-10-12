# Licitações Brasil

Este projeto se chama **Licitações Brasil**, e esse é o único nome a usar em mensagens, recaps,
commits, textos de interface, docstrings e documentação. Em identificadores técnicos, use o slug
`licitacoes-brasil` (pacotes, repositório, URLs) ou `licitacoes_brasil` (nomes Python/SQL).
Commits são assinados como `Licitações-Brasil`.
Exceção (white-label): documentos gerados em nome do licitante (minutas, petições, memoriais, relatórios em PDF/DOCX)
não levam a marca da plataforma; saem no papel timbrado da empresa ativa da conta (ou neutros, sem empresa).

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
  (busca e painel) com itens e anexos oficiais do PNCP e realce dos termos pesquisados. O front pede primeiro
  `?sincronizar=false` (dados gravados, imediato) e depois itens/anexos; falha do PNCP vira `pncp_indisponivel`
  e "Itens ainda não sincronizados" (nunca derruba o drawer). Nas telas, o PNCP usa timeout curto, disjuntor de
  120 s após timeout e não segura conexão do banco durante a espera (`pncp_compras`, `interativo=True`).
- `server/services/sync_items.py`: itens em camadas (`get_or_sync_tender_items`): banco local → PNCP (6 s) →
  Compras.gov.br Dados Abertos (`2.1_consultarItensContratacoes_PNCP_14133_Id` por numeroControlePNCP; cobre compras
  do SIASG), gravando em lote (`TenderItem.origem` PNCP/COMPRAS). A rotina diurna pré-carrega itens de certames
  com sessão em 72 h, favoritos, AL e dispensas. O "+ Negócio" abre a gaveta de triagem (`#deal-items-modal`):
  itens selecionados (`item_ids`, valor = soma) ou modo global (`modo: "global"`, valor do edital).
- `server/services/empresas.py` + `/api/minhas-empresas`: empresas (CNPJs) da conta, cadastradas pela Receita
  (BrasilAPI) e também ao importar certidões no Cofre. Uma empresa = ativa automaticamente; 2+ = seletor "Empresa
  Ativa" no cabeçalho. A ativa define timbre (`timbre`), qualificação (`dados_licitante`), CNPJ de referência e o
  filtro do Cofre (docs da empresa ativa + sem CNPJ). `/api/empresas` continua sendo o apelido de `/api/fornecedores`.
- `server/services/indices_contabeis.py` + `/api/juridico/indices-contabeis`: LG, SG e LC (art. 69), truncados em
  2 casas, e o memorial DOCX timbrado para o contador. Nunca sugere valores de balanço.
- `server/services/minutas.py` + `/api/minutas`: declarações e proposta readequada (Lei 14.133/2021) em DOCX.
- `server/services/certidoes.py` + `POST /api/documents/auto-import/{cnpj}`: pacote de habilitação por CNPJ no
  Cofre. Só entra válido o que foi consultado de fato (situação cadastral via BrasilAPI; CEIS/CNEP com
  `PORTAL_TRANSPARENCIA_API_KEY`); CND, CRF e CNDT entram PENDENTES com link oficial até o PDF ser anexado
  (`POST /api/documentos/{id}/anexar`). O Cofre é privado por conta (`DocumentoEmpresa.user_id`).
- `server/services/document_ai.py` + `POST /api/documents/processar-lote`: upload em lote no Cofre com classificação
  pelo Gemini Flash (JSON Schema estrito; taxonomia da Lei 14.133 em `TIPOS`). Prazos/situação são recalculados no
  servidor; sem chave ou com o Gemini fora do ar, classificador por regras (`metodo` = "regras"). Encaixe em slot
  (`tipo_documento` de slot único: pendente, vencido ou versão anterior) ou documento novo; CNPJ conferido com o da
  importação por CNPJ. Resultado em `DocumentoEmpresa.metadados_ia` (JSONB). Arquivos do Cofre ficam no banco
  (`arquivos_cofre`, `caminho_arquivo` = "db:<id>"), não no disco efêmero do Render.
- `server/services/gemini_chaves.py`: todas as chamadas ao Gemini (resumo, chat, análise de edital, Cofre) passam
  pelo `ClienteRotativo`: chaves em `GEMINI_API_KEYS` (vírgulas; sem ela, `GEMINI_API_KEY`), round-robin por pedido,
  e em erro de cota (429/RESOURCE_EXHAUSTED) a chave fica em espera (`retryDelay` ou 60 s) e o pedido é refeito com a
  próxima. Só sobe erro (`CotaGeminiEsgotada`) quando todas falham no mesmo ciclo.
- `server/services/dossie.py` + `GET /api/fornecedores/dossie/{cnpj}`: Dossiê 360° (Receita/QSA, contratos do
  PNCP filtrados por `fornecedor_ni`, sanções, selo de aptidão), cache de 24 h em `dossies_fornecedor`.
- `server/routes/webhooks.py`: webhook da Hotmart (modo de espera: registra eventos, não altera contas).
- `server/services/dispute_monitor.py`: Sala de Disputa (lotes e robô de lances, por usuário), com ranking Top 10
  informado pelo operador a partir do portal (`POST /api/disputes/ranking/{item}`).
- `server/routes/disputa.py` + `server/services/estrategia_lances.py`: Mesa de Lances do Operador, exclusiva de
  ADMIN (403 para as demais contas), assim como o robô de lances antigo (toggle-global, bid, ligar auto_lance). Cálculo de menor perda (cobre o alvo pelo degrau, nunca joga no piso;
  "melhor colocação viável" do 1º ao 10º) e registro em `admin_bids` (piso obrigatório; abaixo do piso: 422).
  O servidor não envia lances ao portal: o operador dá o lance no portal e o registra.
- `scripts/robo_desktop/` (`main.py` + `build.bat` → `RoboLances_LicitacoesBrasil.exe`): Robô de Lances Desktop,
  exclusivo de ADMIN, que roda na máquina do operador e é o único componente que envia lances ao portal (modo
  padrão `simulacao`: não envia nada). Usa `GET /api/disputa/{item}/worker-config`, `POST .../worker-heartbeat`
  (devolve `abortar` com o Kill-Switch do cockpit, `POST .../kill-switch`) e `POST .../worker-log` (auditoria em
  `historico_disputas`/`admin_bids`; lance abaixo do piso → 422 e Kill-Switch). Travas: Stop Loss antes de
  qualquer envio, parada por ESPAÇO/Q, lances suspensos após 6 s sem contato. Sem `config.json` válido, um
  assistente no terminal (6 passos, `--configurar` para refazer) grava a configuração. Com o robô online, o servidor não
  simula lance automático em memória (`PregaoItem.worker_online`).
- `server/templates/`: páginas (busca, painel, funil, agenda, disputa, jurídico).

## Regras do produto

- **Ambiente de produção, sem dados fictícios.** Nenhum certame, conta, negócio, evento, lote de
  disputa ou concorrente pode ser inventado. Sem resposta da API oficial, a coleta não insere nada.
  Dados de exemplo existem apenas na suíte de testes (`seed_dados_de_teste`, `tests/pncp_simulado.py`).
  Única exceção: o sandbox de disputa (`server/services/simulacao_disputa.py`, `POST /api/disputa/criar-cenarios-simulacao`),
  só para ADMIN, só em memória na conta dele, salas `sim-01` a `sim-04` marcadas `simulado` e concorrentes virtuais
  ("Empresa Alfa", "Beta Distribuidora"). Nada dele vai para o banco, e o robô desktop nunca envia lance real nessas
  salas (`worker-config` devolve `portal: "simulacao"`).
- **Etapas do funil** (`ETAPAS_FUNIL` em server/models.py, fonte única do Kanban e do painel): Oportunidades,
  Qualificação, Proposta Feita (`proposta`), Disputa e Homologado / Ganho (`homologado`). As antigas
  `classificacao`/`contrato` são migradas no boot para `homologado` e ainda aceitas pela API (`normalizar_etapa`).
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
