import unicodedata
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session, defer
from sqlalchemy import or_, and_, desc, asc, func
from server.models import Tender, TenderItem, Source, Favorite
from server.schemas import SearchFilterParams, TenderSummaryResponse, SearchResultResponse
from server.connectors.pncp import build_canonical_pncp_url

# Paginação defensiva (Render: 512 MB). A busca ordena e pagina no banco e só a página pedida vira objeto.
PAGE_SIZE_PADRAO = 25
PAGE_SIZE_MAXIMO = 50
# Ordenação por relevância: pontua apenas as N correspondências mais recentes, com colunas leves
RELEVANCIA_MAX_CANDIDATOS = 1000
# Colunas pesadas que a listagem não exibe (resumos da IA e JSON bruto de itens)
SEM_COLUNAS_PESADAS = (
    defer(Tender.ai_resumo), defer(Tender.ai_pontos_atencao), defer(Tender.itens_extraidos_json),
)

# Situações que encerram ou interrompem a disputa (comparação por trecho, sem acento)
STATUS_NAO_VIGENTES = (
    "suspens", "anulad", "revogad", "cancelad", "encerrad", "desert", "fracassad", "homologad", "conclu",
)

try:
    from zoneinfo import ZoneInfo
    _SP_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - ambiente sem tzdata
    _SP_TZ = timezone(timedelta(hours=-3))


def agora_sao_paulo_naive() -> datetime:
    """Agora em America/Sao_Paulo sem tzinfo: os prazos do PNCP são gravados em horário de Brasília."""
    return datetime.now(_SP_TZ).replace(tzinfo=None)


def _parse_data_iso(valor: Optional[str]):
    """'YYYY-MM-DD' -> date (None se vazio ou inválido)."""
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def filtro_publicacao(date_from: Optional[str] = None, date_to: Optional[str] = None):
    """
    Condição SQL estrita sobre a data oficial de publicação no PNCP (dataPublicacaoPncp, gravada em
    horário de Brasília em Tender.publication_date) — nunca a data de inserção/sincronização local.
    O intervalo é fechado nos dias informados: [date_from 00:00, date_to + 1 dia 00:00).
    """
    inicio, fim = _parse_data_iso(date_from), _parse_data_iso(date_to)
    condicoes = []
    if inicio:
        condicoes.append(Tender.publication_date >= datetime.combine(inicio, datetime.min.time()))
    if fim:
        condicoes.append(Tender.publication_date < datetime.combine(fim + timedelta(days=1), datetime.min.time()))
    return and_(*condicoes) if condicoes else None


def filtro_vigentes(agora: Optional[datetime] = None):
    """
    Condição SQL de certame vigente/aberto para disputa: status ativo e prazo FUTURO para entrega de
    propostas (dataEncerramentoProposta do PNCP; sem ela, a data de abertura). Não há corte pela data
    de publicação: um edital publicado há 45 ou 60 dias com propostas abertas continua vigente.
    Sem nenhuma data de prazo informada, o certame não é contado como vigente.
    """
    agora = agora or agora_sao_paulo_naive()
    status_norm = func.lower(func.coalesce(Tender.status, ""))
    status_ativo = and_(*[~status_norm.like(f"%{trecho}%") for trecho in STATUS_NAO_VIGENTES])
    prazo_aberto = or_(
        Tender.proposal_deadline >= agora,
        and_(Tender.proposal_deadline.is_(None), Tender.opening_date >= agora),
    )
    return and_(status_ativo, prazo_aberto)


# Categorias oficiais de classificação
CATEGORIES = [
    "Medicamentos",
    "Materiais hospitalares",
    "Materiais odontológicos",
    "Equipamentos",
    "Serviços",
    "Tecnologia",
    "Obras",
    "Alimentação",
    "Limpeza",
    "Outros"
]

# As 27 unidades federativas (26 estados + Distrito Federal), em ordem alfabética de sigla
UFS_BRASIL = [
    ("AC", "Acre"), ("AL", "Alagoas"), ("AM", "Amazonas"), ("AP", "Amapá"), ("BA", "Bahia"),
    ("CE", "Ceará"), ("DF", "Distrito Federal"), ("ES", "Espírito Santo"), ("GO", "Goiás"),
    ("MA", "Maranhão"), ("MG", "Minas Gerais"), ("MS", "Mato Grosso do Sul"), ("MT", "Mato Grosso"),
    ("PA", "Pará"), ("PB", "Paraíba"), ("PE", "Pernambuco"), ("PI", "Piauí"), ("PR", "Paraná"),
    ("RJ", "Rio de Janeiro"), ("RN", "Rio Grande do Norte"), ("RO", "Rondônia"), ("RR", "Roraima"),
    ("RS", "Rio Grande do Sul"), ("SC", "Santa Catarina"), ("SE", "Sergipe"), ("SP", "São Paulo"),
    ("TO", "Tocantins"),
]
SIGLAS_UF = {sigla for sigla, _ in UFS_BRASIL}

# Filtro simplificado de categoria (natureza do objeto)
NATUREZAS_FILTRO = [("MATERIAL", "Material"), ("SERVICO", "Serviço"), ("OUTROS", "Outros")]
_NATUREZAS_VALIDAS = {codigo for codigo, _ in NATUREZAS_FILTRO}
_ALIASES_NATUREZA = {
    "MATERIAL": "MATERIAL", "MATERIAIS": "MATERIAL", "M": "MATERIAL",
    "SERVICO": "SERVICO", "SERVIÇO": "SERVICO", "SERVICOS": "SERVICO", "SERVIÇOS": "SERVICO", "S": "SERVICO",
    "OUTROS": "OUTROS", "OUTRO": "OUTROS",
}


def normalizar_natureza(valor: Optional[str]) -> Optional[str]:
    """Converte 'Material', 'serviço', 'SERVICOS'... no código MATERIAL / SERVICO / OUTROS (ou None)."""
    if not valor:
        return None
    return _ALIASES_NATUREZA.get(valor.strip().upper())


def normalizar_uf(valor: Optional[str]) -> Optional[str]:
    if not valor:
        return None
    sigla = valor.strip().upper()
    return sigla if sigla in SIGLAS_UF else None


def lista_param(valores) -> List[str]:
    """Junta parâmetros repetidos (?esfera=A&esfera=B) e separados por vírgula (?esfera=A,B), sem duplicar."""
    if not valores:
        return []
    if isinstance(valores, str):
        valores = [valores]
    saida: List[str] = []
    for valor in valores:
        for parte in str(valor or "").split(","):
            parte = parte.strip().upper()
            if parte and parte not in saida:
                saida.append(parte)
    return saida


# Esfera administrativa: o PNCP grava esferaId (F, E, M, D, N); registros antigos usam o nome por extenso.
ESFERAS_FILTRO = [
    ("FEDERAL", "Federal"), ("ESTADUAL", "Estadual"), ("MUNICIPAL", "Municipal"),
    ("DISTRITAL", "Distrital"), ("OUTROS", "Outros / Consórcios"),
]
_VALORES_ESFERA = {
    "FEDERAL": ("F", "FEDERAL"),
    "ESTADUAL": ("E", "ESTADUAL"),
    "MUNICIPAL": ("M", "MUNICIPAL"),
    "DISTRITAL": ("D", "DISTRITAL"),
}
_ESFERA_POR_VALOR = {v: esfera for esfera, valores in _VALORES_ESFERA.items() for v in valores}


def normalizar_esfera(valor: Optional[str]) -> Optional[str]:
    """'F', 'Federal', 'MUNICIPAL', 'N'... -> FEDERAL / ESTADUAL / MUNICIPAL / DISTRITAL / OUTROS."""
    if not valor or not str(valor).strip():
        return None
    texto = normalize_text(str(valor)).upper()
    if texto in ("OUTROS", "OUTRO", "N", "CONSORCIO", "CONSORCIOS"):
        return "OUTROS"
    return _ESFERA_POR_VALOR.get(texto) or ("OUTROS" if texto else None)


def filtro_esferas(esferas: List[str]):
    escolhidas = [e for e in (normalizar_esfera(x) for x in esferas) if e]
    if not escolhidas:
        return None
    condicoes = []
    conhecidos = [v for valores in _VALORES_ESFERA.values() for v in valores]
    for esfera in dict.fromkeys(escolhidas):
        if esfera == "OUTROS":
            condicoes.append(or_(Tender.administrative_sphere.is_(None), ~func.upper(Tender.administrative_sphere).in_(conhecidos)))
        else:
            condicoes.append(func.upper(Tender.administrative_sphere).in_(_VALORES_ESFERA[esfera]))
    return or_(*condicoes)


# Benefício ME/EPP (LC 123/2006): calculado a partir dos itens oficiais do PNCP (ver pncp_compras)
BENEFICIOS_FILTRO = [("ME_EPP", "Exclusivo ME/EPP"), ("AMPLA", "Ampla Concorrência")]


def filtro_beneficios(beneficios: List[str]):
    condicoes = []
    if "ME_EPP" in beneficios:
        condicoes.append(Tender.itens_exclusivos_me_epp > 0)
    if "AMPLA" in beneficios:
        condicoes.append(Tender.itens_ampla_concorrencia > 0)
    return or_(*condicoes) if condicoes else None


# Portais de disputa, reconhecidos pelo domínio do link oficial da sala (linkSistemaOrigem do PNCP)
PORTAIS_DISPUTA = [
    ("COMPRAS_GOV", "Compras.gov.br", ("comprasnet", "compras.gov.br", "gov.br/compras", "estaleiro.serpro.gov.br")),
    ("BNC", "Bolsa Nacional de Compras (BNC)", ("bnccompras.com",)),
    ("BLL", "BLL Compras", ("bllcompras.com",)),
    ("LICITANET", "Licitanet", ("licitanet.com.br",)),
    ("PORTAL_COMPRAS_PUBLICAS", "Portal de Compras Públicas", ("portaldecompraspublicas.com.br",)),
    ("LICITAR_DIGITAL", "Licitar Digital", ("licitardigital.com.br",)),
    ("LICITACOES_E", "Licitações-e (Banco do Brasil)", ("licitacoes-e.com.br",)),
    ("BANRISUL", "Pregão Banrisul", ("pregaobanrisul.com.br",)),
    ("COMPRAS_MG", "Compras MG", ("compras.mg.gov.br",)),
    ("BEC_SP", "BEC/SP", ("bec.sp.gov.br", "bec.fazenda.sp.gov.br")),
    ("COMPRASBR", "ComprasBR", ("comprasbr.com.br",)),
    ("AMM_LICITA", "AMM Licita", ("ammlicita.org.br",)),
]
_PORTAIS_POR_CODIGO = {codigo: (nome, dominios) for codigo, nome, dominios in PORTAIS_DISPUTA}


def portal_de_disputa(url: Optional[str]) -> Optional[dict]:
    """Portal da sala de disputa a partir do link oficial; None para o próprio PNCP ou link ausente."""
    link = (url or "").strip().lower()
    if not link.startswith("http") or "pncp.gov.br" in link:
        return None
    for codigo, nome, dominios in PORTAIS_DISPUTA:
        if any(d in link for d in dominios):
            return {"codigo": codigo, "nome": nome}
    try:
        from urllib.parse import urlparse
        host = (urlparse(link).hostname or "").removeprefix("www.")
    except ValueError:
        host = ""
    return {"codigo": "OUTRO", "nome": host or "Portal do órgão"} if host else None


def filtro_portais(portais: List[str]):
    condicoes = []
    for codigo in portais:
        if codigo in _PORTAIS_POR_CODIGO:
            _, dominios = _PORTAIS_POR_CODIGO[codigo]
            condicoes.extend(Tender.url_disputa.ilike(f"%{d}%") for d in dominios)
    return or_(*condicoes) if condicoes else None


UF_TO_REGION = {
    "AC": "Norte", "AP": "Norte", "AM": "Norte", "PA": "Norte", "RO": "Norte", "RR": "Norte", "TO": "Norte",
    "AL": "Nordeste", "BA": "Nordeste", "CE": "Nordeste", "MA": "Nordeste", "PB": "Nordeste",
    "PE": "Nordeste", "PI": "Nordeste", "RN": "Nordeste", "SE": "Nordeste",
    "DF": "Centro-Oeste", "GO": "Centro-Oeste", "MT": "Centro-Oeste", "MS": "Centro-Oeste",
    "ES": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "SP": "Sudeste",
    "PR": "Sul", "RS": "Sul", "SC": "Sul"
}

def normalize_text(text: str) -> str:
    """Remove acentos, converte para minúsculas e remove pontuações excessivas"""
    if not text:
        return ""
    # Decomposição canônica e remoção de marcas diacríticas
    nfkd = unicodedata.normalize('NFKD', text)
    cleaned = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return cleaned.lower().strip()

def parse_search_query(query_str: str) -> Tuple[List[str], List[str]]:
    """
    Separa a busca em:
    - expressões exatas (entre aspas duplas: "materiais hospitalares")
    - termos individuais
    """
    if not query_str:
        return [], []
    
    # Extrair frases exatas
    exact_phrases = re.findall(r'"([^"]*)"', query_str)
    # Remover as frases da query principal
    remaining = re.sub(r'"[^"]*"', '', query_str)
    
    # Termos soltos
    terms = [normalize_text(t) for t in remaining.split() if len(t.strip()) > 1]
    normalized_exact = [normalize_text(p) for p in exact_phrases if p.strip()]
    
    return normalized_exact, terms

def pontuar_relevancia(title: Optional[str], object_description: Optional[str], contracting_entity: Optional[str],
                       itens_casados, exact_phrases: List[str], terms: List[str]) -> float:
    """
    Calcula pontuação de relevância de 0.0 a 100.0:
    - Coincidência no título: peso 40
    - Coincidência no objeto: peso 35
    - Coincidência no órgão: peso 15
    - Coincidência nos itens: peso 10 (`itens_casados`: frases/termos encontrados nos itens do certame)
    """
    if not exact_phrases and not terms:
        return 1.0

    score = 0.0
    norm_title = normalize_text(title)
    norm_obj = normalize_text(object_description)
    norm_entity = normalize_text(contracting_entity)

    for phrase in exact_phrases:
        if phrase in norm_title: score += 40.0
        if phrase in norm_obj: score += 30.0
        if phrase in norm_entity: score += 15.0
        if phrase in itens_casados: score += 15.0

    for term in terms:
        if term in norm_title: score += 15.0
        if term in norm_obj: score += 10.0
        if term in norm_entity: score += 5.0
        if term in itens_casados: score += 5.0

    return min(score, 100.0)


def calculate_relevance_score(tender: Tender, exact_phrases: List[str], terms: List[str]) -> float:
    if not exact_phrases and not terms:
        return 1.0
    items_text = " ".join([normalize_text(i.description) for i in (tender.items or [])])
    casados = {p for p in exact_phrases + terms if p in items_text}
    return pontuar_relevancia(tender.title, tender.object_description, tender.contracting_entity,
                              casados, exact_phrases, terms)


def _itens_que_casam(db: Session, ids: List[str], termos: List[str]) -> dict:
    """{termo: ids de certames com item que contém o termo}, consultado no banco sem carregar os itens."""
    casam = {}
    for termo in dict.fromkeys(termos):
        encontrados = set()
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            encontrados.update(r[0] for r in db.query(TenderItem.tender_id).filter(
                TenderItem.tender_id.in_(lote), TenderItem.description.ilike(f"%{termo}%")).distinct())
        casam[termo] = encontrados
    return casam


def _ordem_sql(sort_by: str):
    publicacao = func.coalesce(Tender.publication_date, Tender.created_at)
    prazo = func.coalesce(Tender.proposal_deadline, Tender.opening_date, Tender.created_at)
    valor = func.coalesce(Tender.estimated_total_value, 0.0)
    ordem = {
        "pub_asc": publicacao.asc(),
        "deadline_asc": prazo.asc(),
        "deadline_desc": prazo.desc(),
        "value_desc": valor.desc(),
        "value_asc": valor.asc(),
    }.get(sort_by, publicacao.desc())
    return ordem, Tender.id.asc()

def search_tenders(db: Session, params: SearchFilterParams, user_id: Optional[str] = None) -> SearchResultResponse:
    """
    Mecanismo completo de busca com normalização de acentos,
    expressões exatas, múltiplos termos e filtros combináveis.
    """
    query = db.query(Tender)

    # 1. Filtros Estruturados
    uf_sel = (params.state or params.uf or "").strip().upper()
    if uf_sel:
        query = query.filter(Tender.state == uf_sel)

    if params.city:
        query = query.filter(Tender.city.ilike(f"%{params.city.strip()}%"))

    if params.region:
        query = query.filter(Tender.region.ilike(f"%{params.region.strip()}%"))

    if params.contracting_entity:
        query = query.filter(Tender.contracting_entity.ilike(f"%{params.contracting_entity.strip()}%"))

    cond_esfera = filtro_esferas(lista_param(params.esfera) + lista_param(params.administrative_sphere))
    if cond_esfera is not None:
        query = query.filter(cond_esfera)

    cond_beneficio = filtro_beneficios(lista_param(params.beneficio))
    if cond_beneficio is not None:
        query = query.filter(cond_beneficio)

    cond_portal = filtro_portais(lista_param(params.portal_disputa))
    if cond_portal is not None:
        query = query.filter(cond_portal)

    if params.modality:
        m_term = params.modality.strip()
        if "dispensa" in m_term.lower():
            query = query.filter(Tender.modality.ilike("%Dispensa%"))
        else:
            query = query.filter(Tender.modality.ilike(f"%{m_term}%"))

    if params.status:
        query = query.filter(Tender.status.ilike(f"%{params.status.strip()}%"))

    # Categoria simplificada (MATERIAL / SERVICO / OUTROS); valores legados seguem filtrando Tender.category
    natureza_sel = normalizar_natureza(params.natureza) or normalizar_natureza(params.category)
    if natureza_sel:
        query = query.filter(Tender.natureza_objeto == natureza_sel)
    elif params.category:
        query = query.filter(Tender.category.ilike(f"%{params.category.strip()}%"))

    if params.source_system:
        query = query.filter(Tender.source_system == params.source_system.strip().lower())

    if params.portal:
        p_term = params.portal.strip()
        if p_term.lower() in ("compras.gov.br", "compras.gov", "compras_gov", "comprasnet"):
            query = query.filter(
                or_(
                    Tender.source_system == "compras_gov",
                    Tender.sicaf_ready == True,
                    Tender.raw_data_reference.ilike("%Compras.gov.br%")
                )
            )
        elif p_term.lower() in ("pncp geral", "pncp"):
            query = query.filter(
                and_(
                    Tender.source_system == "pncp",
                    ~Tender.raw_data_reference.ilike("%Compras.gov.br%")
                )
            )
        else:
            query = query.filter(
                or_(
                    Tender.raw_data_reference.ilike(f"%{p_term}%"),
                    Tender.official_notice_url.ilike(f"%{p_term}%"),
                    Tender.source_url.ilike(f"%{p_term}%")
                )
            )

    if params.uasg_code:
        query = query.filter(Tender.uasg_code == params.uasg_code.strip())

    if params.sicaf_ready is not None:
        query = query.filter(Tender.sicaf_ready == params.sicaf_ready)

    if params.value_min is not None:
        query = query.filter(Tender.estimated_total_value >= params.value_min)

    if params.value_max is not None:
        query = query.filter(Tender.estimated_total_value <= params.value_max)

    cond_publicacao = filtro_publicacao(params.date_from, params.date_to)
    if cond_publicacao is not None:
        query = query.filter(cond_publicacao)

    if params.vigentes:
        query = query.filter(filtro_vigentes())

    # 2. Busca Textual por Palavra-chave / Expressão Exata
    exact_phrases, terms = [], []
    if params.q:
        exact_phrases, terms = parse_search_query(params.q)
        text_conditions = []
        
        for phrase in exact_phrases:
            text_conditions.append(Tender.title.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.object_description.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.contracting_entity.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.items.any(TenderItem.description.ilike(f"%{phrase}%")))

        for term in terms:
            text_conditions.append(Tender.title.ilike(f"%{term}%"))
            text_conditions.append(Tender.object_description.ilike(f"%{term}%"))
            text_conditions.append(Tender.contracting_entity.ilike(f"%{term}%"))
            text_conditions.append(Tender.process_number.ilike(f"%{term}%"))
            text_conditions.append(Tender.notice_number.ilike(f"%{term}%"))
            text_conditions.append(Tender.items.any(TenderItem.description.ilike(f"%{term}%")))

        if text_conditions:
            query = query.filter(or_(*text_conditions))

    # Obter lista de favoritos do usuário logado
    user_fav_ids = set()
    if user_id:
        favs = db.query(Favorite.tender_id).filter(Favorite.user_id == user_id).all()
        user_fav_ids = {f[0] for f in favs}

    # 3. Ordenação e paginação no banco (LIMIT/OFFSET): nunca carrega o catálogo inteiro em memória
    total = query.order_by(None).count()
    effective_page_size = params.limit if params.limit is not None else params.page_size
    effective_page_size = max(1, min(effective_page_size, PAGE_SIZE_MAXIMO))
    total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
    start_idx = (params.page - 1) * effective_page_size
    termos_busca = exact_phrases + terms

    if params.sort_by == "relevance" and termos_busca:
        # Pontua só colunas leves das correspondências mais recentes; os itens são conferidos no SQL
        candidatos = (query.with_entities(Tender.id, Tender.title, Tender.object_description, Tender.contracting_entity)
                      .order_by(*_ordem_sql("pub_desc")).limit(RELEVANCIA_MAX_CANDIDATOS).all())
        casam = _itens_que_casam(db, [c.id for c in candidatos], termos_busca)
        pontuados = [
            (c.id, pontuar_relevancia(c.title, c.object_description, c.contracting_entity,
                                      {tm for tm in termos_busca if c.id in casam[tm]}, exact_phrases, terms))
            for c in candidatos
        ]
        del candidatos
        pontuados.sort(key=lambda x: x[1], reverse=True)  # estável: empates seguem a publicação mais recente
        pagina = pontuados[start_idx:start_idx + effective_page_size]
        por_id = {t.id: t for t in db.query(Tender).options(*SEM_COLUNAS_PESADAS)
                  .filter(Tender.id.in_([i for i, _ in pagina]))}
        page_slice = [(por_id[i], score) for i, score in pagina if i in por_id]
    else:
        pagina = (query.options(*SEM_COLUNAS_PESADAS).order_by(*_ordem_sql(params.sort_by))
                  .offset(start_idx).limit(effective_page_size).all())
        casam = _itens_que_casam(db, [t.id for t in pagina], termos_busca) if termos_busca else {}
        page_slice = [
            (t, pontuar_relevancia(t.title, t.object_description, t.contracting_entity,
                                   {tm for tm in termos_busca if t.id in casam[tm]}, exact_phrases, terms))
            for t in pagina
        ]

    results = []
    for t, score in page_slice:
        source_name = t.source.name if t.source else "PNCP"
        executor_portal = None
        if t.raw_data_reference:
            try:
                import json
                ref_json = json.loads(t.raw_data_reference)
                if isinstance(ref_json, dict):
                    executor_portal = ref_json.get("executor_portal")
            except Exception:
                pass
        if not executor_portal:
            executor_portal = "Compras.gov.br" if (t.source_system == "compras_gov" or t.sicaf_ready) else "PNCP Geral"

        results.append(TenderSummaryResponse(
            id=t.id,
            external_id=t.external_id,
            process_number=t.process_number or "Não informado",
            notice_number=t.notice_number or "Não informado",
            title=t.title,
            object_description=t.object_description,
            contracting_entity=t.contracting_entity,
            purchasing_unit=t.purchasing_unit or "Não informado",
            state=t.state,
            city=t.city or "Não informado",
            administrative_sphere=t.administrative_sphere,
            modality=t.modality,
            category=t.category or "Outros",
            natureza_objeto=t.natureza_objeto,
            status=t.status or "Não informado",
            publication_date=t.publication_date,
            opening_date=t.opening_date,
            proposal_deadline=t.proposal_deadline,
            estimated_total_value=t.estimated_total_value,
            source_name=source_name,
            source_url=t.source_url,
            official_notice_url=t.official_notice_url,
            source_system=t.source_system or "pncp",
            uasg_code=t.uasg_code,
            sicaf_ready=bool(t.sicaf_ready),
            link_comprasnet=t.link_comprasnet,
            link_pncp=(
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            url_disputa=getattr(t, "url_disputa", None) or t.link_comprasnet or t.official_notice_url or (
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            url_pncp=(
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            executor_portal=executor_portal,
            portal_disputa=(portal_de_disputa(t.url_disputa) or {}).get("nome"),
            esfera=normalizar_esfera(t.administrative_sphere),
            itens_exclusivos_me_epp=t.itens_exclusivos_me_epp,
            itens_ampla_concorrencia=t.itens_ampla_concorrencia,
            data_completeness=t.data_completeness,
            is_favorite=(t.id in user_fav_ids),
            match_score=round(score, 1) if (exact_phrases or terms) else None,
            created_at=t.created_at,
            last_synced_at=t.last_synced_at
        ))

    return SearchResultResponse(
        total=total,
        page=params.page,
        page_size=effective_page_size,
        total_pages=total_pages,
        results=results,
        applied_filters=params.model_dump(exclude_none=True)
    )
