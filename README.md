# Licitações Brasil 🇧🇷
### Plataforma Web Inteligente de Pesquisa, Monitoramento e Análise Documental de Licitações Públicas Brasileiras

Uma solução completa, segura, escalável e moderna para pesquisa, gestão e inteligência documental sobre contratações públicas no Brasil, integrada ao **Portal Nacional de Contratações Públicas (PNCP)** e em estrita conformidade com a **Nova Lei de Licitações (Lei nº 14.133/2021)** e com a **Lei Geral de Proteção de Dados (LGPD - Lei nº 13.709/2018)**.

---

## 1. Visão Geral e Arquitetura

A plataforma combina uma interface web responsiva e acessível com um poderoso motor de busca facetada, pipelines de validação e ingestão de arquivos PDF de editais, deduplicação de certames oficiais e um módulo de auditoria documental por Inteligência Artificial baseado no **Google Gemini** (com suporte multi-modelo e camada de abstração para Claude e OpenAI).

### Diagrama Arquitetural em Camadas

```
┌────────────────────────────────────────────────────────────────────────┐
│                   INTERFACE DE USUÁRIO / FRONTEND                      │
│      Next.js 14/15 App Router | React | Tailwind CSS | Jinja2 Live     │
│  - Busca Avançada & Filtros Combináveis (UF, Município, Valor, etc.)  │
│  - Visualização Detalhada em Abas (Itens, Documentos, IA, Histórico)  │
│  - Dashboard de Oportunidades & Painel Administrativo de Fontes        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP / JSON REST API
┌───────────────────────────────────▼────────────────────────────────────┐
│                    CAMADA DE APLICAÇÃO & SERVIÇOS                      │
│               FastAPI (Python 3.12) & Next.js Server Actions           │
│  ├── Autenticação Segura (Bcrypt, JWT HS256, Anti-enumeração, RBAC)    │
│  ├── Motor de Busca (Normalização de Acentos, Expressão Exata, Rank)   │
│  ├── Pipeline de PDFs (Validação Magic Bytes, SHA-256, Extração Texto) │
│  ├── Motor de IA Documental (Gemini 3.5 Flash / 3.8, Blindagem Prompt) │
│  └── Motor de Alertas & Radares (Deduplicação de Notificações, In-App) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ SQLAlchemy / Prisma ORM
┌───────────────────────────────────▼────────────────────────────────────┐
│                  BANCO DE DADOS & PERSISTÊNCIA                         │
│  PostgreSQL 16 (Produção Docker) / SQLite (Desenvolvimento Zero-Config) │
│  - Users, Sources, Tenders, TenderItems, TenderDocuments, Favorites    │
│  - UserNotes, SavedSearches, Alerts, AlertDeliveries, AIAnalyses       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│             CONECTORES DE FONTES PÚBLICAS & GOVERNAMENTAIS             │
│  ├── PNCP API v1 (Portal Nacional de Contratações Públicas)            │
│  ├── Compras.gov.br (Comprasnet)                                       │
│  ├── Portais Estaduais (Compras RS, Compras MG, Compras Bahia, etc.)   │
│  └── Plataformas Autorizadas (Portal de Compras Públicas, BLL Compras) │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Funcionalidades Implementadas

### 2.1 Cadastro, Autenticação & LGPD
- **Registro e Login:** Hash de senhas exclusivamente via **Bcrypt** com salt criptográfico de 12 rounds.
- **Sessões e Tokens:** JWT assinado com chave criptográfica simétrica segura e controle de expiração.
- **Defesa contra Enumeração:** Respostas temporais uniformizadas e mensagens genéricas para evitar enumeração de contas por atacantes.
- **Controle de Acesso (RBAC):** Níveis de permissão `USER` (pesquisador/fornecedor) e `ADMIN` (gestor da plataforma).
- **Direito ao Esquecimento (LGPD Art. 18):** Exclusão integral da conta e anonimização de notas pessoais via endpoint `DELETE /api/auth/account`.

### 2.2 Motor de Busca & Relevância
- **Normalização de Acentos e Minúsculas:** Decomposição canônica NFKD (`unaccent`), permitindo que buscas como `"licitação"`, `"medicamento"` e `"farmácia"` encontrem certames grafados com ou sem acentos.
- **Busca por Expressão Exata:** Termos entre aspas (ex: `"materiais médico-hospitalares"`) são avaliados como frases contíguas prioritárias.
- **Busca por Múltiplos Termos:** Cruzamento de termos no título, objeto detalhado, código do certame e descrições dos itens.
- **Algoritmo de Relevância Ponderada:** Score de 0 a 100 pontos priorizando título (40%), objeto (35%), itens (15%) e órgão (10%).
- **Filtros Combináveis:**
  - **Estado (UF)** (27 unidades federativas).
  - **Município** e Região geográfica.
  - **Esfera Administrativa** (Federal, Estadual, Municipal).
  - **Modalidade:** Pregão Eletrônico, Concorrência, Dispensa Eletrônica, Inexigibilidade, Credenciamento, Leilão.
  - **Categorias Pré-configuradas:** Medicamentos, Materiais hospitalares, Materiais odontológicos, Equipamentos, Serviços, Tecnologia, Obras, Alimentação, Limpeza, Outros.
  - **Faixa de Valor Estimado:** Mínimo e Máximo (R$).
  - **Cronograma e Datas:** Período personalizado de publicação e prazo final de envio de propostas.
  - **Tratamento de Dados Incompletos:** Campos ausentes exibem expressamente `"Não informado"` em vez de suposições.

### 2.3 Detalhes do Edital em Abas
- **Visão Geral:** Metadados completos, cronograma, condições de pagamento e local de entrega.
- **Itens:** Tabela com número do item, descrição técnica, unidade, quantidade, valor unitário e total estimado, e código CATMAT/CATSER.
- **Documentos:** Lista de arquivos oficiais, tipo, mime-type, tamanho e **Checksum SHA-256** para verificação de autenticidade.
- **Análise por IA:** Resumo executivo, informações comerciais, requisitos de habilitação (jurídica, fiscal, técnica), pontos de atenção, prazos críticos e citações com número de página do edital.
- **Histórico de Alterações:** Auditoria de retificações, prorrogações e esclarecimentos.
- **Observações Particulares:** Bloco de anotações da equipe do usuário para estratégias de lances.

### 2.4 Integração com o PNCP (Portal Nacional de Contratações Públicas)
- Conector baseado na **API v1 oficial** (`https://pncp.gov.br/api/consulta/swagger-ui/index.html`).
- Mapeamento direto de `numeroControlePNCP`, `orgaoEntidade`, `modalidadeId`, `valorTotalEstimado` e `linkSistemaOrigem`.
- **Deduplicação Nativa:** Restrição de unicidade em banco por `(source_id, external_id)`.
- **Tratamento de Limitações de Rede:** Caso o ambiente de execução possua restrição de saída (egress firewall retornando HTTP 403), o sistema detecta a indisponibilidade, registra logs estruturados e aciona o espelho local auditado com dados reais da SESAU/AL, PRODESP/SP, Ministério da Saúde/DF, SEE/MG e SOP/RS.

### 2.5 Processamento de PDFs e IA Documental
- Validação estrita de cabeçalho (`%PDF-`) e limite de tamanho para proteção contra exaustão de memória.
- Extração página por página preservando o número da página para geração de citações confiáveis (`[Página X]`).
- Detecção de PDFs escaneados por análise de densidade de caracteres.
- **Blindagem contra Prompt Injection:** Sanitização de padrões hostis (`IGNORE ALL PREVIOUS INSTRUCTIONS`, instruções de override de sistema) e encapsulamento em delimitador seguro `<untrusted_document_context>`.
- **Aviso Legal Obrigatório:** Ressalva expressa de que a IA auxilia a interpretação documental, mas não substitui a conferência integral do edital oficial nem constitui parecer jurídico definitivo.

### 2.6 Alertas, Histórico & Deduplicação
- Configuração de alertas por palavra-chave, estado (UF), categoria e canal (notificação interna ou e-mail).
- Motor de avaliação periódica que registra cada disparo em `AlertDelivery`, evitando notificações duplicadas.

---

## 3. Estrutura de Pastas do Projeto

```
licitacoes-brasil/
├── app/                       # Aplicação Next.js 14/15 (App Router)
│   ├── (auth)/                # Rotas de autenticação (login, register)
│   ├── dashboard/             # Painel de controle do usuário
│   ├── search/                # Página de busca avançada
│   ├── tenders/[id]/          # Detalhes do certame em abas
│   ├── favorites/             # Gestão de favoritos
│   ├── alerts/                # Gestão de radares e alertas
│   ├── admin/                 # Painel administrativo
│   ├── layout.tsx             # Layout global com Header e Footer
│   └── globals.css            # Estilos globais Tailwind CSS
├── components/                # Componentes React reutilizáveis
│   ├── Navbar.tsx             # Barra de navegação responsiva
│   └── Footer.tsx             # Rodapé com notas legais e LGPD
├── integrations/              # Conectores de fontes governamentais
│   ├── pncp/                  # Cliente oficial PNCP (Swagger v1)
│   └── sources/               # Catálogo de portais estaduais/municipais
├── lib/                       # Bibliotecas e utilitários TypeScript
│   ├── search/                # Normalização e parser de queries
│   └── security/              # Utilitários de segurança
├── prisma/                    # Modelagem ORM e Migrations
│   ├── schema.prisma          # Schema relacional completo
│   └── seed.ts                # Script de povoamento inicial
├── server/                    # Motor Web e API FastAPI de Alto Desempenho
│   ├── main.py                # Ponto de entrada FastAPI e rotas REST
│   ├── config.py              # Gestão central de configurações (.env)
│   ├── database.py            # Sessão e engine SQLAlchemy
│   ├── models.py              # Modelos relacionais (1:1 com Prisma)
│   ├── schemas.py             # Schemas Pydantic v2 com validação estrita
│   ├── security.py            # Bcrypt, JWT, Rate limiting, Prompt injection defense
│   ├── search_engine.py       # Motor de busca, ranking e relevância
│   ├── pdf_processor.py       # Pipeline de validação e chunking de PDFs
│   ├── ai_engine.py           # Abstração de IA (Gemini 3.5 / Gemini 3.8)
│   ├── alerts_engine.py       # Disparador e deduplicador de alertas
│   ├── seed_data.py           # Dados autênticos para demonstração imediata
│   ├── connectors/            # Implementação dos conectores de fontes
│   └── templates/             # Templates Jinja2 responsivos e dinâmicos
├── tests/                     # Suíte de Testes Automatizados (pytest)
│   ├── conftest.py            # Fixture para setup e sementes do banco
│   ├── test_search.py         # Testes de busca, acentos e filtros
│   ├── test_auth.py           # Testes de autenticação, hashing e JWT
│   ├── test_pdf.py            # Testes do leitor e chunker de PDF
│   ├── test_ai.py             # Testes da blindagem de prompt e esquema de IA
│   ├── test_alerts.py         # Testes de regras e deduplicação de alertas
│   ├── test_security.py       # Testes de rate limiting e exclusão LGPD
│   └── test_api_endpoints.py  # Testes de integração de endpoints REST
├── Dockerfile                 # Construção Docker multi-stage
├── docker-compose.yml         # Orquestração de App, PostgreSQL e Redis
├── package.json               # Dependências do ecossistema Next.js/Node
├── requirements.txt           # Dependências do motor Python/FastAPI
├── .env.example               # Documentação completa de variáveis
├── .env                       # Configuração ativa local
└── README.md                  # Este documento
```

---

## 4. Instruções de Execução

### Opção A: Execução Imediata via Python / Uvicorn (Ambiente Local / Sandbox)

1. **Instalar dependências:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Iniciar o servidor da aplicação:**
   ```bash
   export PYTHONPATH=/licitacoes-brasil
   python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000
   ```

3. **Acessar a aplicação no navegador:**
   - **Interface Web:** [http://localhost:8000](http://localhost:8000)
   - **Documentação Interativa Swagger:** [http://localhost:8000/api/docs](http://localhost:8000/api/docs)
   - **Documentação ReDoc:** [http://localhost:8000/api/redoc](http://localhost:8000/api/redoc)

4. **Credenciais Pré-configuradas:**
   - **Usuário Comum:** `usuario@empresa.com.br` | Senha: `UsuarioSenha@123`
   - **Administrador:** `admin@licitacoesbrasil.gov.br` | Senha: `AdminSeguro!2025`

---

### Opção B: Execução via Docker Compose (PostgreSQL + Redis + App)

1. **Iniciar todos os serviços isolados em contêineres:**
   ```bash
   docker compose up --build -d
   ```

2. **Verificar os serviços e logs:**
   ```bash
   docker compose ps
   docker compose logs -f app
   ```

3. **Acessar:**
   - [http://localhost:8000](http://localhost:8000)

---

## 5. Execução dos Testes Automatizados

A plataforma possui uma suíte com **21 testes automatizados** abrangendo unidades, integrações, regras de negócio e segurança.

Para executar todos os testes:
```bash
export PYTHONPATH=/licitacoes-brasil
pytest tests/ -v
```

### Resultados dos Testes Executados:
```
tests/test_ai.py::test_prompt_injection_sanitization PASSED              [  4%]
tests/test_ai.py::test_ai_analysis_structure PASSED                      [  9%]
tests/test_alerts.py::test_alert_matching_rules PASSED                   [ 14%]
tests/test_alerts.py::test_alert_job_deduplication PASSED                [ 19%]
tests/test_api_endpoints.py::test_api_health_endpoint PASSED             [ 23%]
tests/test_api_endpoints.py::test_api_search_endpoint PASSED             [ 28%]
tests/test_api_endpoints.py::test_api_login_success PASSED               [ 33%]
tests/test_api_endpoints.py::test_api_login_invalid_password PASSED      [ 38%]
tests/test_api_endpoints.py::test_api_sources_list PASSED                [ 42%]
tests/test_auth.py::test_password_hashing_and_verification PASSED        [ 47%]
tests/test_auth.py::test_jwt_token_flow PASSED                           [ 52%]
tests/test_auth.py::test_user_enumeration_protection PASSED              [ 57%]
tests/test_pdf.py::test_pdf_processing_pipeline PASSED                   [ 61%]
tests/test_pdf.py::test_pdf_invalid_bytes PASSED                         [ 66%]
tests/test_pdf.py::test_pdf_chunking_with_citations PASSED               [ 71%]
tests/test_search.py::test_text_normalization PASSED                     [ 76%]
tests/test_search.py::test_query_parsing PASSED                          [ 80%]
tests/test_search.py::test_search_alagoas_healthcare_scenario PASSED     [ 85%]
tests/test_search.py::test_search_value_range_filter PASSED              [ 90%]
tests/test_security.py::test_rate_limiting_sliding_window PASSED         [ 95%]
tests/test_security.py::test_lgpd_account_deletion_flow PASSED           [100%]

======================= 21 passed in 16.03s =======================
```

---

## 6. Exemplo de Pesquisa Prática

O sistema atende diretamente ao cenário de referência solicitado:
> **"Encontre licitações abertas em Alagoas para medicamentos, materiais médico-hospitalares e produtos para farmácias."**

1. Acesse `/search` ou clique no botão rápido na Home:
   - Termo: `medicamentos materiais hospitalares farmácias`
   - Estado: `AL`
   - Categoria: `Medicamentos`
2. O motor de busca retorna instantaneamente o certame oficial **PE 042/2025** da **SESAU/AL**, com valor de R$ 4.850.000,00, contendo dipirona, seringas e luvas, documentos técnicos anexos e botão de **Análise por IA** para auditoria de cláusulas e habilitações.
