import os
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from server.models import (
    User, Source, Tender, TenderItem, TenderDocument,
    DocumentExtraction, AIAnalysis, Alert, SavedSearch,
    LegalDocumentTemplate
)
from server.config import settings
from server.security import hash_password, calculate_sha256

# Contas e certames de exemplo antigos: não são mais criados. Os identificadores ficam
# registrados para que a migração apague o que ainda existir em bancos antigos.
EMAILS_CONTAS_DEMONSTRACAO = ("admin@licitacoesbrasil.gov.br", "usuario@empresa.com.br")
IDS_CERTAMES_EXEMPLO = (
    "tnd-al-saude-001", "tnd-sp-ti-002", "tnd-df-equip-003", "tnd-mg-alim-004", "tnd-rs-obras-005",
)


ID_FONTE_PNCP = "pncp-source-01"


def garantir_fonte_pncp(db: Session) -> Source:
    """Fonte oficial do PNCP, à qual pertencem todos os certames coletados da API pública."""
    fonte = db.get(Source, ID_FONTE_PNCP)
    if not fonte:
        fonte = Source(
            id=ID_FONTE_PNCP,
            name="Portal Nacional de Contratações Públicas (PNCP)",
            base_url="https://pncp.gov.br/api/consulta",
            official_domain="pncp.gov.br",
            source_type="FEDERAL",
            integration_type="API_OFICIAL",
            active=True,
            authorization_notes="Portal central instituído pelo artigo 174 da Lei Federal nº 14.133/2021.",
            last_sync_at=datetime.utcnow(),
            last_sync_status="SUCESSO"
        )
        db.add(fonte)
        db.flush()
    return fonte


def seed_database(db: Session):
    """
    Dados essenciais de produção: fontes oficiais (PNCP / Compras.gov.br) e modelos de peças
    jurídicas. Nenhuma conta, certame, negócio, alerta ou evento de exemplo é criado.
    """
    # 1. Fontes Oficiais
    garantir_fonte_pncp(db)

    comprasgov_source = db.query(Source).filter(Source.id == "comprasgov-source-02").first()
    if not comprasgov_source:
        comprasgov_source = Source(
            id="comprasgov-source-02",
            name="Compras.gov.br (Comprasnet)",
            base_url="https://comprasgovernamentais.gov.br",
            official_domain="gov.br",
            source_type="FEDERAL",
            integration_type="API_OFICIAL",
            active=True,
            authorization_notes="Sistema de compras governamentais do Governo Federal.",
            last_sync_at=datetime.utcnow(),
            last_sync_status="SUCESSO"
        )
        db.add(comprasgov_source)

    db.commit()

    # 2. Modelos de peças jurídicas operacionais
    if db.query(LegalDocumentTemplate).count() == 0:
        templates_seed = [
            {
                "tipo": "impugnacao",
                "titulo": "Impugnação ao Edital por Exigência Restritiva",
                "fundamentacao_legal": "Art. 164 da Lei Federal nº 14.133/2021 e Art. 37, XXI da CF/88",
                "is_nova_lei": True,
                "corpo": """ILMO. SR. PREGOEIRO / AGENTE DE CONTRATAÇÃO DO(A) {{orgao}}

REF.: EDITAL DE LICITAÇÃO Nº {{numero_edital}}
OBJETO: {{objeto}}

[NOME DA EMPRESA REQUERENTE], inscrita no CNPJ sob o nº [CNPJ], com sede em [ENDEREÇO], vem, respeitosamente, com fulcro no art. 164 da Lei Federal nº 14.133/2021, apresentar tempestiva

IMPUGNAÇÃO AO EDITAL

em face das exigências restritivas contidas no Instrumento Convocatório, pelos fatos e fundamentos a seguir expostos:

1. DA TEMPESTIVIDADE
A presente peça é tempestiva, protocolada até 3 (três) dias úteis antes da data de abertura do certame, conforme preconiza expressamente o art. 164 da Lei nº 14.133/2021.

2. DOS FATOS E DO DIRECIONAMENTO
O Edital nº {{numero_edital}}, cujo objeto é {{objeto}}, inseriu cláusulas técnicas desproporcionais que restringem injustificadamente o universo de competidores, violando os princípios da competitividade e isonomia (art. 5º da Lei 14.133/2021).

3. DOS PEDIDOS
Diante do exposto, requer-se:
a) O recebimento e processamento da presente impugnação;
b) O julgamento PROCEDENTE para fins de retificar o Edital nº {{numero_edital}}, republicando-se o certame com a devida reabertura de prazos.

Nestes termos,
Pede deferimento.
[CIDADE - UF], [DATA]."""
            },
            {
                "tipo": "esclarecimento",
                "titulo": "Pedido de Esclarecimento sobre Especificações Técnicas",
                "fundamentacao_legal": "Art. 164 da Lei Federal nº 14.133/2021",
                "is_nova_lei": True,
                "corpo": """ILMO. SR. AGENTE DE CONTRATAÇÃO DO(A) {{orgao}}

REF.: EDITAL Nº {{numero_edital}}
OBJETO: {{objeto}}

A empresa [NOME DA EMPRESA], pessoa jurídica de direito privado, inscrita no CNPJ nº [CNPJ], formula o presente

PEDIDO DE ESCLARECIMENTO

com fulcro no art. 164 da Lei nº 14.133/2021:

1. DO OBJETO E DA DÚVIDA SUSCITADA
Referente ao certame nº {{numero_edital}}, cujo objeto é {{objeto}}, verificou-se divergência pontual entre o Termo de Referência e as planilhas orçamentárias.

2. DO QUESTIONAMENTO OBJETIVO
Indaga-se a esta d. Administração se a especificação técnica admitirá soluções similares ou equivalentes comprovadas em catálogo, em respeito ao art. 41, I da Lei 14.133/2021.

Termos em que,
Aguarda tempestiva resposta e publicação das notas de esclarecimento."""
            },
            {
                "tipo": "intencao_recurso",
                "titulo": "Manifestação Motivada de Intenção Recursal em Sessão",
                "fundamentacao_legal": "Art. 165, § 1º, I da Lei Federal nº 14.133/2021",
                "is_nova_lei": True,
                "corpo": """AO ILMO. PREGOEIRO DO(A) {{orgao}}
SESSÃO PÚBLICA DO EDITAL Nº {{numero_edital}}
OBJETO: {{objeto}}

MANIFESTAÇÃO MOTIVADA DE INTENÇÃO DE RECURSO

A licitante [NOME DA EMPRESA] manifesta tempestivamente nesta sessão sua INTENÇÃO DE RECORRER em face da decisão de julgamento e habilitação do certame.

MOTIVAÇÃO SUCINTA:
A proposta declarada vencedora para o certame {{numero_edital}} ({{objeto}}) descumpriu exigências mandatórias do instrumento convocatório quanto à qualificação técnica e demonstração de exequibilidade, violando o princípio da vinculação ao edital e o art. 59 da Lei 14.133/2021. As razões recursais completas serão acostadas no prazo legal de 3 (três) dias úteis."""
            },
            {
                "tipo": "recurso",
                "titulo": "Razões de Recurso Administrativo contra Julgamento",
                "fundamentacao_legal": "Art. 165, I, 'a' e 'b' da Lei Federal nº 14.133/2021",
                "is_nova_lei": True,
                "corpo": """ILMO. SR. AGENTE DE CONTRATAÇÃO / AUTORIDADE COMPETENTE DO(A) {{orgao}}

PROCESSO LICITATÓRIO: EDITAL Nº {{numero_edital}}
OBJETO: {{objeto}}

[RECORRENTE], já devidamente qualificada nos autos do certame nº {{numero_edital}}, vem apresentar tempestivas

RAZÕES DE RECURSO ADMINISTRATIVO

1. DA TEMPESTIVIDADE
Apresentadas dentro do tríduo legal estipulado pelo art. 165, § 1º da Lei 14.133/2021.

2. DAS RAZÕES DE REFORMA
A decisão proferida no certame nº {{numero_edital}} concernente a {{objeto}} merece integral reforma, posto que desconsiderou vício formal insanável na documentação de habilitação da licitante concorrente.

3. DOS PEDIDOS
Requer-se o conhecimento do recurso e reconsideração da decisão, ou encaminhamento à autoridade superior para provimento integral, declarando-se a recorrente vencedora."""
            },
            {
                "tipo": "contrarbenzoes",
                "titulo": "Contrarrazões ao Recurso Administrativo da Concorrente",
                "fundamentacao_legal": "Art. 165, § 2º da Lei Federal nº 14.133/2021",
                "is_nova_lei": True,
                "corpo": """ILMO. SR. PREGOEIRO / AGENTE DE CONTRATAÇÃO DO(A) {{orgao}}

REF.: EDITAL Nº {{numero_edital}}
OBJETO: {{objeto}}

[RECORRIDA], vencedora legítima do certame, vem apresentar tempestivas

CONTRARRAZÕES DE RECURSO ADMINISTRATIVO

em face do recurso infundado interposto pela licitante inconformada.

1. DA MANUTENÇÃO DA DECISÃO
A decisão que declarou vencedora a peticionária no certame nº {{numero_edital}} relativo a {{objeto}} observou rigorosamente os princípios da legalidade e vantajocidade da Lei 14.133/2021.

2. DO PEDIDO
Requer-se a manutenção da decisão recorrida, negando-se provimento ao recurso da concorrente e adjudicando-se o objeto."""
            },
            {
                "tipo": "impugnacao",
                "titulo": "Impugnação ao Edital (Regime Lei 8.666/1993 - Legado)",
                "fundamentacao_legal": "Art. 41 da Lei Federal nº 8.666/1993",
                "is_nova_lei": False,
                "corpo": """ILMO. SR. PRESIDENTE DA COMISSÃO PERMANENTE DE LICITAÇÃO DO(A) {{orgao}}

REF.: EDITAL Nº {{numero_edital}}
OBJETO: {{objeto}}

A empresa requerente apresenta IMPUGNAÇÃO AO EDITAL com base no art. 41 da Lei Federal nº 8.666/1993, apontando obscuridades e exigências descabidas na contratação de {{objeto}}.

Requer a retificação do edital."""
            }
        ]

        for t_data in templates_seed:
            t_obj = LegalDocumentTemplate(
                tipo=t_data["tipo"],
                titulo=t_data["titulo"],
                fundamentacao_legal=t_data["fundamentacao_legal"],
                corpo=t_data["corpo"],
                is_nova_lei=t_data["is_nova_lei"],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(t_obj)
        db.commit()

    # 3. Administrador e usuário iniciais opcionais, definidos por variáveis de ambiente
    garantir_admin_inicial(db)
    garantir_usuario_inicial(db)


def garantir_usuario_inicial(db: Session) -> None:
    """
    Se USUARIO_INICIAL_EMAIL estiver configurado e a conta não existir, cria-a com papel USER
    usando USUARIO_INICIAL_SENHA (mín. 8 caracteres). Conta existente não é alterada: a senha,
    o nome e o papel ficam como o próprio usuário (ou um administrador) deixou.
    """
    email = (settings.USUARIO_INICIAL_EMAIL or "").strip().lower()
    senha = settings.USUARIO_INICIAL_SENHA or ""
    if not email or len(senha) < 8 or db.query(User).filter(User.email == email).first():
        return
    db.add(User(
        name=(settings.USUARIO_INICIAL_NOME or "").strip() or email.split("@")[0],
        email=email,
        password_hash=hash_password(senha),
        role="USER",
        status="ACTIVE",
    ))
    db.commit()


def garantir_admin_inicial(db: Session) -> None:
    """
    Se ADMIN_EMAIL estiver configurado: atualiza a conta existente (papel ADMIN, conta ativa e,
    com ADMIN_NAME, o nome) ou, com ADMIN_PASSWORD, cria a conta. A senha de uma conta existente
    nunca é sobrescrita. Sem as variáveis, não cria nenhuma conta padrão.
    """
    email = (settings.ADMIN_EMAIL or "").strip().lower()
    if not email:
        return
    nome = (settings.ADMIN_NAME or "").strip()
    user = db.query(User).filter(User.email == email).first()
    if user:
        alterado = False
        if user.role != "ADMIN":
            user.role, alterado = "ADMIN", True
        if user.status != "ACTIVE":
            user.status, alterado = "ACTIVE", True
        if nome and user.name != nome:
            user.name, alterado = nome, True
        if alterado:
            db.commit()
        return
    senha = settings.ADMIN_PASSWORD or ""
    if len(senha) < 8:
        return
    db.add(User(
        name=nome or "Administrador",
        email=email,
        password_hash=hash_password(senha),
        role="ADMIN",
        status="ACTIVE",
    ))
    db.commit()


def seed_dados_de_teste(db: Session):
    """
    SOMENTE PARA A SUÍTE DE TESTES (tests/conftest.py): contas e certames de exemplo com
    itens, documento e alerta. Nunca é chamado pela aplicação.
    """
    # 1. Usuários (contas exclusivas da suíte, em domínio de exemplo)
    admin = db.query(User).filter(User.email == "admin.testes@exemplo.com.br").first()
    if not admin:
        admin = User(
            id="usr-admin-01",
            name="Administrador de Testes",
            email="admin.testes@exemplo.com.br",
            password_hash=hash_password("SenhaTesteAdmin@2026"),
            role="ADMIN",
            status="ACTIVE"
        )
        db.add(admin)

    demo_user = db.query(User).filter(User.email == "usuario.testes@exemplo.com.br").first()
    if not demo_user:
        demo_user = User(
            id="usr-user-01",
            name="Usuário de Testes",
            email="usuario.testes@exemplo.com.br",
            password_hash=hash_password("SenhaTesteUsuario@2026"),
            role="USER",
            status="ACTIVE"
        )
        db.add(demo_user)

    db.commit()

    # 2. Certames de exemplo com itens e documento (Alagoas / Saúde, SP, DF, MG, RS)
    now = datetime.utcnow()

    sample_tenders = [
        {
            "id": "tnd-al-saude-001",
            "source_id": "pncp-source-01",
            "external_id": "12200135000180-1-000042/2025",
            "process_number": "E:02000.00001234/2025",
            "notice_number": "PE 042/2025",
            "title": "Registro de Preços para aquisição de medicamentos, materiais médico-hospitalares e produtos para farmácias hospitalares",
            "object_description": "Registro de Preços para futura e eventual aquisição de medicamentos básicos e especiais, materiais médico-hospitalares descartáveis (seringas, agulhas, cateteres, luvas) e insumos para abastecimento da rede de farmácias públicas e unidades de emergência do Estado de Alagoas.",
            "contracting_entity": "Secretaria de Estado da Saúde de Alagoas - SESAU/AL",
            "purchasing_unit": "Gerência Executiva de Suprimentos e Logística Hospitalar",
            "state": "AL",
            "city": "Maceió",
            "region": "Nordeste",
            "administrative_sphere": "ESTADUAL",
            "modality": "Pregão Eletrônico",
            "category": "Medicamentos",
            "status": "Divulgada no PNCP",
            "publication_date": now - timedelta(days=2),
            "opening_date": now + timedelta(days=10),
            "proposal_deadline": now + timedelta(days=10, hours=9),
            "estimated_total_value": 4850000.00,
            "source_url": "https://pncp.gov.br/app/editais/12200135000180-1-000042/2025",
            "official_notice_url": "https://pncp.gov.br/app/editais/12200135000180-1-000042/2025",
            "data_completeness": "COMPLETO",
            "items": [
                {
                    "item_number": 1,
                    "description": "Dipirona Monoidratada 500mg/mL - Solução Injetável 2mL",
                    "unit": "AMPOLA",
                    "quantity": 150000,
                    "estimated_unit_value": 1.45,
                    "estimated_total_value": 217500.00,
                    "classification_code": "CATMAT-271828"
                },
                {
                    "item_number": 2,
                    "description": "Luva para Procedimento Não Cirúrgico - Látex com pó, Tamanho M",
                    "unit": "CAIXA COM 100 UN",
                    "quantity": 40000,
                    "estimated_unit_value": 26.50,
                    "estimated_total_value": 1060000.00,
                    "classification_code": "CATMAT-314159"
                },
                {
                    "item_number": 3,
                    "description": "Amoxicilina + Clavulanato de Potássio 500mg + 125mg - Comprimido Revestido",
                    "unit": "COMPRIMIDO",
                    "quantity": 200000,
                    "estimated_unit_value": 2.10,
                    "estimated_total_value": 420000.00,
                    "classification_code": "CATMAT-281920"
                },
                {
                    "item_number": 4,
                    "description": "Seringa descartável 5mL com agulha 25x7 - Bico luer lock estéril",
                    "unit": "UNIDADE",
                    "quantity": 300000,
                    "estimated_unit_value": 0.48,
                    "estimated_total_value": 144000.00,
                    "classification_code": "CATMAT-192837"
                }
            ],
            "document_sample": {
                "name": "Edital_PE_042_2025_Medicamentos_SESAU_AL.pdf",
                "document_type": "Edital e Termo de Referência",
                "text": """ESTADO DE ALAGOAS - SECRETARIA DE ESTADO DA SAÚDE (SESAU/AL)
EDITAL DE PREGÃO ELETRÔNICO Nº 042/2025
PROCESSO ADMINISTRATIVO: E:02000.00001234/2025
CRITÉRIO DE JULGAMENTO: Menor Preço por Item
MODO DE DISPUTA: Aberto e Fechado

1. DO OBJETO
1.1. O presente certame tem por objeto o Registro de Preços para aquisição continuada de medicamentos essenciais, materiais médico-hospitalares e insumos para dispensação em farmácias hospitalares da rede estadual de Alagoas, com vigência da ata de 12 meses.

2. DAS CONDIÇÕES DE PARTICIPAÇÃO E HABILITAÇÃO
2.1. Habilitação Jurídica: Contrato Social devidamente arquivado na Junta Comercial e Cartão CNPJ com CNAE compatível com comércio atacadista de produtos farmacêuticos.
2.2. Regularidade Fiscal e Trabalhista: CND Federal, CND Estadual (Alagoas), CND Municipal, CRF-FGTS e CNDT emitida pelo TST.
2.3. Qualificação Técnica: Apresentação de Autorização de Funcionamento de Empresa (AFE) emitida pela ANVISA e Alvará Sanitário vigente emitido pela Vigilância Sanitária local. Apresentação de Certidão de Regularidade Técnica perante o CRF.
2.4. Atestados de Capacidade Técnica comprovando fornecimento prévio de ao menos 50% dos quantitativos licitados.

3. DO LOCAL E PRAZO DE ENTREGA
3.1. As entregas deverão ocorrer no Almoxarifado Central da SESAU, situado em Maceió/AL, no prazo máximo de 10 (dez) dias corridos após o recebimento da Ordem de Fornecimento.

4. DAS CONDIÇÕES DE PAGAMENTO
4.1. O pagamento será processado em até 30 (trinta) dias após a apresentação da Nota Fiscal acompanhada do respectivo Termo de Recebimento Definitivo.

5. DAS PENALIDADES E DISPOSIÇÕES FINAIS
5.1. O atraso injustificado sujeitará a contratada à multa moratória de 0,5% por dia de atraso, limitada a 15% do valor do pedido."""
            }
        },
        {
            "id": "tnd-sp-ti-002",
            "source_id": "pncp-source-01",
            "external_id": "46379400000150-1-000115/2025",
            "process_number": "PRO-SP-2025/0091",
            "notice_number": "PE 115/2025",
            "title": "Contratação de serviços de nuvem governamental, computação em nuvem e suporte técnico especializado",
            "object_description": "Contratação de empresa especializada na prestação de serviços de computação em nuvem pública (Cloud Computing), arquitetura multinuvem, migração de dados e sustentação continuada para a infraestrutura digital do Estado de São Paulo.",
            "contracting_entity": "Companhia de Processamento de Dados do Estado de São Paulo - PRODESP",
            "purchasing_unit": "Diretoria de Operações e Tecnologia",
            "state": "SP",
            "city": "São Paulo",
            "region": "Sudeste",
            "administrative_sphere": "ESTADUAL",
            "modality": "Pregão Eletrônico",
            "category": "Tecnologia",
            "status": "Divulgada no PNCP",
            "publication_date": now - timedelta(days=4),
            "opening_date": now + timedelta(days=12),
            "proposal_deadline": now + timedelta(days=12, hours=10),
            "estimated_total_value": 14200000.00,
            "source_url": "https://pncp.gov.br/app/editais/46379400000150-1-000115/2025",
            "official_notice_url": "https://pncp.gov.br/app/editais/46379400000150-1-000115/2025",
            "data_completeness": "COMPLETO",
            "items": [
                {
                    "item_number": 1,
                    "description": "Unidades de Serviço de Nuvem (USN) para Instâncias Computacionais e Storage",
                    "unit": "USN",
                    "quantity": 500000,
                    "estimated_unit_value": 24.00,
                    "estimated_total_value": 12000000.00,
                    "classification_code": "CATSER-29381"
                },
                {
                    "item_number": 2,
                    "description": "Horas de Consultoria Técnica Especializada em Engenharia DevSecOps",
                    "unit": "HORA",
                    "quantity": 8000,
                    "estimated_unit_value": 275.00,
                    "estimated_total_value": 2200000.00,
                    "classification_code": "CATSER-30112"
                }
            ],
            "document_sample": {
                "name": "Termo_Referencia_PE_115_2025_Nuvem_PRODESP.pdf",
                "document_type": "Termo de Referência",
                "text": """PRODESP - TECNOLOGIA DA INFORMAÇÃO
TERMO DE REFERÊNCIA - PREGÃO ELETRÔNICO Nº 115/2025
Objeto: Prestação de serviços de Cloud Computing gerenciado com redundância geográfica e certificações ISO 27001 e SOC 2 Tipo II.
Prazos: O início dos serviços deve ocorrer em até 15 dias corridos após a emissão da Ordem de Início.
Local de Entrega: Prestação de serviços de forma remota com reuniões presenciais periódicas na sede da PRODESP em Taboão da Serra/SP."""
            }
        },
        {
            "id": "tnd-df-equip-003",
            "source_id": "pncp-source-01",
            "external_id": "00394452000103-1-000301/2025",
            "process_number": "MS-DF-25000.0881/2025",
            "notice_number": "PE 301/2025",
            "title": "Aquisição de equipamentos médico-hospitalares de alta complexidade para Unidades de Terapia Intensiva (UTI)",
            "object_description": "Aquisição centralizada de ventiladores pulmonares mecânicos microprocessados, monitores multiparamétricos e bombas de infusão contínua para modernização das UTIs dos Hospitais Federais e Universitários.",
            "contracting_entity": "Ministério da Saúde - Departamento de Logística em Saúde (DLOG)",
            "purchasing_unit": "Coordenação-Geral de Aquisições",
            "state": "DF",
            "city": "Brasília",
            "region": "Centro-Oeste",
            "administrative_sphere": "FEDERAL",
            "modality": "Pregão Eletrônico",
            "category": "Equipamentos",
            "status": "Divulgada no PNCP",
            "publication_date": now - timedelta(days=1),
            "opening_date": now + timedelta(days=15),
            "proposal_deadline": now + timedelta(days=15, hours=9),
            "estimated_total_value": 32800000.00,
            "source_url": "https://pncp.gov.br/app/editais/00394452000103-1-000301/2025",
            "official_notice_url": "https://pncp.gov.br/app/editais/00394452000103-1-000301/2025",
            "data_completeness": "COMPLETO",
            "items": [
                {
                    "item_number": 1,
                    "description": "Ventilador Pulmonar Microprocessado para Paciente Adulto, Pediátrico e Neonatal",
                    "unit": "UNIDADE",
                    "quantity": 200,
                    "estimated_unit_value": 115000.00,
                    "estimated_total_value": 23000000.00,
                    "classification_code": "CATMAT-44120"
                },
                {
                    "item_number": 2,
                    "description": "Monitor Multiparamétrico com módulo de capnografia e débito cardíaco",
                    "unit": "UNIDADE",
                    "quantity": 280,
                    "estimated_unit_value": 35000.00,
                    "estimated_total_value": 9800000.00,
                    "classification_code": "CATMAT-44121"
                }
            ],
            "document_sample": {
                "name": "Edital_DLOG_Equipamentos_UTI_2025.pdf",
                "document_type": "Edital",
                "text": """MINISTÉRIO DA SAÚDE - DLOG/SE/MS
EDITAL DE PREGÃO ELETRÔNICO Nº 301/2025
Garantia e Assistência Técnica: Mínimo de 36 (trinta e seis) meses on-site com peças inclusas.
Entrega: Distribuição nos polos logísticos de Brasília/DF, Recife/PE e Rio de Janeiro/RJ em até 60 dias corridos."""
            }
        },
        {
            "id": "tnd-mg-alim-004",
            "source_id": "pncp-source-01",
            "external_id": "18715615000160-1-000088/2025",
            "process_number": "SEE-MG-2025/1120",
            "notice_number": "PE 088/2025",
            "title": "Aquisição de gêneros alimentícios não perecíveis para atendimento ao Programa Nacional de Alimentação Escolar (PNAE)",
            "object_description": "Fornecimento parcelado de gêneros alimentícios (arroz polido tipo 1, feijão carioquinha, óleo de soja, leite em pó integral) para o atendimento nutricional dos estudantes da rede estadual de ensino de Minas Gerais.",
            "contracting_entity": "Secretaria de Estado de Educação de Minas Gerais - SEE/MG",
            "purchasing_unit": "Superintendência de Alimentação Escolar",
            "state": "MG",
            "city": "Belo Horizonte",
            "region": "Sudeste",
            "administrative_sphere": "ESTADUAL",
            "modality": "Pregão Eletrônico",
            "category": "Alimentação",
            "status": "Divulgada no PNCP",
            "publication_date": now - timedelta(days=6),
            "opening_date": now + timedelta(days=8),
            "proposal_deadline": now + timedelta(days=8, hours=14),
            "estimated_total_value": 8940000.00,
            "source_url": "https://pncp.gov.br/app/editais/18715615000160-1-000088/2025",
            "official_notice_url": "https://pncp.gov.br/app/editais/18715615000160-1-000088/2025",
            "data_completeness": "COMPLETO",
            "items": [
                {
                    "item_number": 1,
                    "description": "Arroz Beneficiado Polido Tipo 1 - Fardo com 6 pacotes de 5kg",
                    "unit": "FARDO 30KG",
                    "quantity": 35000,
                    "estimated_unit_value": 165.00,
                    "estimated_total_value": 5775000.00,
                    "classification_code": "CATMAT-10112"
                },
                {
                    "item_number": 2,
                    "description": "Feijão Carioquinha Novo Tipo 1 - Fardo com 10 pacotes de 1kg",
                    "unit": "FARDO 10KG",
                    "quantity": 42000,
                    "estimated_unit_value": 75.35,
                    "estimated_total_value": 3165000.00,
                    "classification_code": "CATMAT-10113"
                }
            ],
            "document_sample": {
                "name": "Edital_PNAE_MG_2025.pdf",
                "document_type": "Edital",
                "text": """ESTADO DE MINAS GERAIS - SECRETARIA DE EDUCAÇÃO
PREGÃO ELETRÔNICO Nº 088/2025 - PNAE
Exigência de laudo bromatológico e sanitário expedido por laboratório credenciado.
Entregas descentralizadas nas 47 Superintendências Regionais de Ensino de Minas Gerais."""
            }
        },
        {
            "id": "tnd-rs-obras-005",
            "source_id": "pncp-source-01",
            "external_id": "87922251000140-1-000018/2025",
            "process_number": "PROC-SOP-2025-0018",
            "notice_number": "CONC 018/2025",
            "title": "Obras de reforma, reforço estrutural e ampliação de unidades escolares e contenção de encostas",
            "object_description": "Contratação de empresa de engenharia para execução de obras de recuperação estrutural, reconstrução de taludes e revitalização de instalações em municípios do Rio Grande do Sul.",
            "contracting_entity": "Secretaria de Obras Públicas do Rio Grande do Sul - SOP/RS",
            "purchasing_unit": "Subsecretaria de Infraestrutura e Obras",
            "state": "RS",
            "city": "Porto Alegre",
            "region": "Sul",
            "administrative_sphere": "ESTADUAL",
            "modality": "Concorrência",
            "category": "Obras",
            "status": "Divulgada no PNCP",
            "publication_date": now - timedelta(days=5),
            "opening_date": now + timedelta(days=22),
            "proposal_deadline": now + timedelta(days=22, hours=14),
            "estimated_total_value": 18500000.00,
            "source_url": "https://pncp.gov.br/app/editais/87922251000140-1-000018/2025",
            "official_notice_url": "https://pncp.gov.br/app/editais/87922251000140-1-000018/2025",
            "data_completeness": "COMPLETO",
            "items": [
                {
                    "item_number": 1,
                    "description": "Serviços de Engenharia Civil e Obras de Arte Especiais - Lote 1 Região Metropolitana",
                    "unit": "GLOBAL",
                    "quantity": 1,
                    "estimated_unit_value": 18500000.00,
                    "estimated_total_value": 18500000.00,
                    "classification_code": "CATSER-50119"
                }
            ],
            "document_sample": {
                "name": "Edital_Concorrencia_018_2025_Obras_SOP_RS.pdf",
                "document_type": "Edital e Projeto Executivo",
                "text": """GOVERNO DO ESTADO DO RIO GRANDE DO SUL
EDITAL DE CONCORRÊNCIA ELETRÔNICA Nº 018/2025
Regime de Execução: Empreitada por Preço Unitário
Garantia Contratual: 5% do valor do contrato nos termos da Lei 14.133/2021."""
            }
        }
    ]

    for item_data in sample_tenders:
        existing = db.query(Tender).filter(Tender.id == item_data["id"]).first()
        if existing:
            continue

        tender = Tender(
            id=item_data["id"],
            source_id=item_data["source_id"],
            external_id=item_data["external_id"],
            process_number=item_data["process_number"],
            notice_number=item_data["notice_number"],
            title=item_data["title"],
            object_description=item_data["object_description"],
            contracting_entity=item_data["contracting_entity"],
            purchasing_unit=item_data["purchasing_unit"],
            state=item_data["state"],
            city=item_data["city"],
            region=item_data["region"],
            administrative_sphere=item_data["administrative_sphere"],
            modality=item_data["modality"],
            category=item_data["category"],
            status=item_data["status"],
            publication_date=item_data["publication_date"],
            opening_date=item_data["opening_date"],
            proposal_deadline=item_data["proposal_deadline"],
            estimated_total_value=item_data["estimated_total_value"],
            source_url=item_data["source_url"],
            official_notice_url=item_data["official_notice_url"],
            source_system=item_data.get("source_system", "compras_gov" if item_data.get("administrative_sphere") == "FEDERAL" else "pncp"),
            uasg_code=item_data.get("uasg_code", "250005" if item_data.get("administrative_sphere") == "FEDERAL" else None),
            sicaf_ready=item_data.get("sicaf_ready", True if item_data.get("administrative_sphere") == "FEDERAL" else False),
            link_comprasnet=item_data.get("link_comprasnet", "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=250005" if item_data.get("administrative_sphere") == "FEDERAL" else None),
            link_pncp=item_data.get("link_pncp", item_data.get("source_url")),
            data_completeness=item_data["data_completeness"]
        )
        db.add(tender)
        db.flush()

        # Adicionar Itens
        for it in item_data.get("items", []):
            tender_item = TenderItem(
                tender_id=tender.id,
                item_number=it["item_number"],
                description=it["description"],
                unit=it["unit"],
                quantity=it["quantity"],
                estimated_unit_value=it["estimated_unit_value"],
                estimated_total_value=it["estimated_total_value"],
                classification_code=it.get("classification_code")
            )
            db.add(tender_item)

        # Adicionar Documento e Extração
        doc_info = item_data.get("document_sample")
        if doc_info:
            doc = TenderDocument(
                tender_id=tender.id,
                name=doc_info["name"],
                document_type=doc_info["document_type"],
                source_url=tender.source_url,
                mime_type="application/pdf",
                file_size=len(doc_info["text"].encode("utf-8")),
                checksum=calculate_sha256(doc_info["text"].encode("utf-8")),
                published_at=tender.publication_date,
                extraction_status="SUCCESS"
            )
            db.add(doc)
            db.flush()

            extraction = DocumentExtraction(
                document_id=doc.id,
                extraction_status="SUCCESS",
                extracted_text_reference=doc_info["text"],
                extracted_text_hash=doc.checksum,
                page_count=2,
                ocr_used=False
            )
            db.add(extraction)

    # 3. Alerta de exemplo do usuário de teste (Alagoas / Saúde)
    sample_alert = db.query(Alert).filter(Alert.user_id == demo_user.id).first()
    if not sample_alert:
        sample_alert = Alert(
            id="alt-al-saude-01",
            user_id=demo_user.id,
            name="Alerta Saúde Alagoas (Medicamentos e Hospitalar)",
            keywords="medicamentos hospitalar farmácia",
            filters={"state": "AL", "category": "Medicamentos"},
            frequency="DAILY",
            channels="IN_APP,EMAIL",
            active=True
        )
        db.add(sample_alert)

    db.commit()
