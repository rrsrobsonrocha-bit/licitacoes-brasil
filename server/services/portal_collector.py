import os
import re
import json
import logging
import asyncio
import unicodedata
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta
import httpx

from server.connectors.pncp import build_canonical_pncp_url

logger = logging.getLogger(__name__)


# ==============================================================================
# CLASSIFICAÇÃO SIMPLIFICADA DA NATUREZA DO OBJETO (MATERIAL / SERVIÇO / OUTROS)
# ==============================================================================

NATUREZA_MATERIAL = "MATERIAL"
NATUREZA_SERVICO = "SERVICO"
NATUREZA_OUTROS = "OUTROS"
NATUREZAS_OBJETO = (NATUREZA_MATERIAL, NATUREZA_SERVICO, NATUREZA_OUTROS)
NATUREZA_LABELS = {
    NATUREZA_MATERIAL: "Material",
    NATUREZA_SERVICO: "Serviço",
    NATUREZA_OUTROS: "Outros",
}

# Objetos que não são compra nem prestação de serviço (alienações, cessões, imóveis).
_RE_OUTROS_FORTE = re.compile(
    r"\b("
    r"leilao|alienacao|venda de (bens|imoveis|veiculos|sucatas?)|"
    r"locacao de (imovel|imoveis|espaco|sala|galpao|predio)|"
    r"(concessao|permissao|cessao|autorizacao) (onerosa |remunerada )?de uso|"
    r"concessao (comum|patrocinada|administrativa)|parceria publico.privada|"
    r"doacao|patrocinio|chamamento publico para (selecao de )?(organizacao|osc|entidade)"
    r")\b"
)

# Verbo/forma inicial que define a natureza preponderante do objeto.
_PREFIXO_ATA = r"(formalizacao de ata de |constitui objeto da presente licitacao (a |o )?)?"

_RE_LIDER_MATERIAL = re.compile(
    r"^" + _PREFIXO_ATA + r"(registro de precos?\s+(para|visando|objetivando)\s+(a\s+|o\s+)?"
    r"((futura|eventual)(\s+e)?\s+)*)?"
    r"(aquisicao|aquisicoes|compra|compras|fornecimento)\b"
)
# "Contratação de empresa (especializada) no/para fornecimento de ..." é compra de material.
_RE_CONTRATACAO_FORNECIMENTO = re.compile(
    r"contratacao de empresa( especializada| legalmente constituida)?( no ramo de [a-z ]+)?"
    r",? (no |para (o |a )?|que realize o )?(fornecimento|aquisicao)\b"
)
_RE_LIDER_SERVICO = re.compile(
    r"^" + _PREFIXO_ATA + r"(registro de precos?\s+(para|visando|objetivando)\s+(a\s+|o\s+)?"
    r"((futura|eventual)(\s+e)?\s+)*)?"
    r"(prestacao de servicos?|contratacao de (empresa|pessoa juridica|instituicao|servicos?)|"
    r"execucao de (obras?|servicos?)|servicos?)\b"
)

_PADROES_SERVICO = [
    (re.compile(r"\bprestacao de servicos?\b"), 4),
    (re.compile(r"\bservicos?\b"), 2),
    (re.compile(r"\bcontratacao de (empresa|pessoa juridica|instituicao)\b"), 2),
    (re.compile(r"\b(obras?|engenharia|pavimentacao|recapeamento|drenagem|reforma|construcao|ampliacao|restauracao)\b"), 2),
    (re.compile(r"\b(manutencao|conservacao|limpeza|higienizacao|vigilancia|seguranca patrimonial|portaria|zeladoria|jardinagem)\b"), 2),
    (re.compile(r"\b(assistencia tecnica|consultoria|assessoria|auditoria|treinamento|capacitacao|curso|palestra)\b"), 2),
    (re.compile(r"\b(locacao|terceirizacao|mao de obra|dedicacao exclusiva|outsourcing)\b"), 2),
    (re.compile(r"\b(transporte|frete|fretamento|coleta|remocao|destinacao final|agenciamento|passagens aereas)\b"), 1),
    (re.compile(r"\b(suporte tecnico|desenvolvimento de (sistema|software)|hospedagem|telefonia|link de (internet|dados))\b"), 1),
    (re.compile(r"\b(exames|laboratoriais|plantoes|atendimento|organizacao de eventos|publicidade|dedetizacao|desinsetizacao)\b"), 1),
    (re.compile(r"\b(instalacao|execucao|operacao|gestao|gerenciamento|monitoramento)\b"), 1),
    (re.compile(r"\b(adequacao|recuperacao|sustentacao|modernizacao|implantacao|implementacao|desenvolvimento|evolucao|pericia|processamento de dados|nuvem|cloud)\b"), 1),
    (re.compile(r"^contratacao\b"), 1),
]

_PADROES_MATERIAL = [
    (re.compile(r"\b(aquisicao|aquisicoes|compra|fornecimento)\b"), 2),
    (re.compile(r"^" + _PREFIXO_ATA + r"registro de precos?\b"), 1),
    (re.compile(r"\b(material|materiais|medicamentos?|farmacos|insumos?|equipamentos?|generos alimenticios|alimentos)\b"), 2),
    (re.compile(r"\b(pecas|componentes|sobressalentes|acessorios|ferramentas|utensilios|pneus)\b"), 1),
    (re.compile(r"\b(veiculos?|ambulancias?|onibus|caminhoes|motocicletas|maquinas)\b"), 1),
    (re.compile(r"\b(mobiliario|moveis|cadeiras|mesas|armarios|eletrodomesticos|ar.condicionados?)\b"), 1),
    (re.compile(r"\b(computadores|notebooks?|microcomputadores|impressoras|monitores|tablets|licencas?|servidores (de rede|rack)|storage)\b"), 1),
    (re.compile(r"\b(papel|toner|cartuchos|expediente|combustive(l|is)|gasolina|diesel|uniformes|fardamentos?|epis?)\b"), 1),
    (re.compile(r"\b(produtos|kits?|cestas basicas|merenda|livros|imunobiologicos|vacinas|oncologicos|orteses?|proteses?|cateteres?|sondas?|reagentes?|racao)\b"), 1),
]


def _normalizar_texto(texto: str) -> str:
    """Minúsculas, sem acentos e com espaços colapsados para casamento léxico estável."""
    base = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(ch for ch in base if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def classificar_natureza_objeto(objeto: str) -> str:
    """
    Classifica o objeto de um certame em uma das três naturezas simplificadas.

    Retorna estritamente ``"MATERIAL"``, ``"SERVICO"`` ou ``"OUTROS"``.

    Regras (léxicas/regex, determinísticas):
      1. Alienações, leilões, cessões/concessões de uso e locação de imóveis -> OUTROS.
      2. Pontua sinais de material (aquisição, fornecimento, medicamentos, equipamentos...)
         e de serviço (prestação de serviços, manutenção, obras e serviços de engenharia...).
         Obras e serviços de engenharia são agrupados em SERVICO nesta taxonomia simplificada.
      3. O verbo inicial do objeto ("Aquisição de...", "Contratação de empresa para...")
         recebe peso extra, pois define a natureza preponderante.
      4. Empate é decidido pelo verbo inicial; sem nenhum sinal -> OUTROS.
    """
    texto = _normalizar_texto(objeto if isinstance(objeto, str) else "")
    if not texto:
        return NATUREZA_OUTROS

    lider_material = bool(_RE_LIDER_MATERIAL.search(texto) or _RE_CONTRATACAO_FORNECIMENTO.search(texto))
    lider_servico = bool(_RE_LIDER_SERVICO.search(texto)) and not lider_material

    # Alienação/cessão/concessão só prevalece quando é o núcleo do objeto (início do texto),
    # e não um detalhe acessório de uma contratação de serviço ou compra.
    outros = _RE_OUTROS_FORTE.search(texto)
    if outros and outros.start() < 60 and not (lider_material or lider_servico):
        return NATUREZA_OUTROS

    pontos_material = sum(peso for padrao, peso in _PADROES_MATERIAL if padrao.search(texto))
    pontos_servico = sum(peso for padrao, peso in _PADROES_SERVICO if padrao.search(texto))

    if lider_material:
        pontos_material += 3
    if lider_servico:
        pontos_servico += 3

    if pontos_material == 0 and pontos_servico == 0:
        return NATUREZA_OUTROS
    if pontos_material > pontos_servico:
        return NATUREZA_MATERIAL
    if pontos_servico > pontos_material:
        return NATUREZA_SERVICO
    if lider_material and not lider_servico:
        return NATUREZA_MATERIAL
    if lider_servico and not lider_material:
        return NATUREZA_SERVICO
    return NATUREZA_OUTROS


def classificar_natureza_certame(titulo: Optional[str], descricao: Optional[str] = None) -> str:
    """Classifica um certame priorizando o título e recorrendo à descrição quando inconclusivo."""
    natureza = classificar_natureza_objeto(titulo or "")
    if natureza == NATUREZA_OUTROS and descricao:
        natureza_desc = classificar_natureza_objeto(descricao)
        if natureza_desc != NATUREZA_OUTROS or not titulo:
            return natureza_desc
    return natureza

# Sanitização de ambiente para httpx
if "NO_PROXY" in os.environ and "[" in os.environ["NO_PROXY"]:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
if "no_proxy" in os.environ and "[" in os.environ["no_proxy"]:
    os.environ["no_proxy"] = "localhost,127.0.0.1"


DEFAULT_HEADERS = {
    "User-Agent": "licitacoes-brasil-collector/2.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate"
}

def _data_pncp(valor: Any) -> Optional[datetime]:
    """Data/hora oficial do PNCP (horário de Brasília, ISO) em datetime; None se ausente ou inválida."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "").split(".")[0])
    except (TypeError, ValueError):
        return None


class PortalCollectorService:
    """
    Hub agregador multifonte para coleta assíncrona, desduplicação e
    normalização de certames dos principais portais brasileiros:
    - PNCP (Portal Nacional de Contratações Públicas)
    - Compras.gov.br (Comprasnet)
    - Portal de Compras Públicas
    - BNC (Bolsa Nacional de Compras)
    - Licitanet
    - Portais Estaduais (BEC-SP, Compras-MG, etc.)
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    async def fetch_pncp(
        self,
        uf: Optional[str] = None,
        keywords: Optional[str] = None,
        keyword: Optional[str] = None,
        max_results: int = 15,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Consulta editais abertos no PNCP via API oficial."""
        target_kw = keywords or keyword
        target_limit = limit or max_results
        results: List[Dict[str, Any]] = []
        today = datetime.now()
        data_ini = (today - timedelta(days=15)).strftime("%Y%m%d")
        data_fim = (today + timedelta(days=30)).strftime("%Y%m%d")

        url = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
        params: Dict[str, Any] = {
            "dataInicial": data_ini,
            "dataFinal": data_fim,
            "codigoModalidadeContratacao": 6, # Pregão Eletrônico
            "pagina": 1,
            "tamanhoPagina": min(target_limit, 30)
        }
        if uf:
            params["uf"] = uf.upper()

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=DEFAULT_HEADERS) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for it in (items or [])[:target_limit]:
                        raw_ext_id = it.get("numeroControlePNCP") or f"PNCP-{it.get('sequencialCompra')}"
                        canon_url = build_canonical_pncp_url(raw_record=it, numero_controle_pncp=raw_ext_id)
                        orgao = it.get("orgaoEntidade", {})
                        results.append({
                            "external_id": raw_ext_id,
                            "process_number": it.get("processo") or it.get("numeroProcesso"),
                            "notice_number": it.get("numeroCompra"),
                            "title": (it.get("objetoCompra") or it.get("objeto") or "Contratação Pública PNCP")[:500],
                            "object_description": it.get("objetoCompra") or it.get("objeto") or "",
                            "contracting_entity": orgao.get("razaoSocial") or "Órgão Licitante",
                            "purchasing_unit": it.get("unidadeOrgao", {}).get("nomeUnidade") or "Unidade Geral",
                            "state": (it.get("unidadeOrgao", {}).get("ufSigla") or uf or "BR").upper(),
                            "city": it.get("unidadeOrgao", {}).get("municipioNome") or "Não informado",
                            "modality": "Pregão Eletrônico",
                            "status": it.get("situacaoCompraNome") or "Divulgada no PNCP",
                            "estimated_total_value": float(it.get("valorTotalEstimado") or 0.0),
                            "publication_date": _data_pncp(it.get("dataPublicacaoPncp")),
                            "opening_date": _data_pncp(it.get("dataAberturaProposta")),
                            "proposal_deadline": _data_pncp(it.get("dataEncerramentoProposta")),
                            "fonte_portal": "PNCP",
                            "link_edital_oficial": canon_url,
                            "url_disputa": canon_url,
                            "url_pncp": canon_url,
                            "source_url": canon_url
                        })
        except Exception as e:
            logger.warning(f"Coletor PNCP: falha na requisição externa ({e}). Nenhum certame inserido.")

        return results

    async def fetch_compras_gov(self, uasg: Optional[str] = None, keywords: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Compras.gov.br: as contratações publicadas chegam pelo PNCP (fetch_pncp). Sem integração
        direta própria, não devolve registros — nunca certames fictícios.
        """
        return []

    async def fetch_state_and_private_portals(self, portal_name: str, uf: Optional[str] = None, keywords: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Portais estaduais/privados (BNC, Portal de Compras Públicas, Licitanet, BEC-SP): sem API
        oficial integrada, não devolvem registros — nunca certames fictícios.
        """
        return []

    async def sync_pipeline(
        self,
        sources: Optional[List[str]] = None,
        ufs: Optional[List[str]] = None,
        keywords: Optional[str] = None,
        rate_limit_delay: float = 0.1,
        db_session: Optional[Any] = None,
        use_fallback: bool = False
    ) -> Dict[str, Any]:
        """
        Fila assíncrona de sincronização multifonte com controle estrito de rate-limit,
        tratamento de exceções e persistência integrada na base de dados.
        """
        active_sources = sources or ["PNCP", "Compras.gov.br", "Portal de Compras Públicas", "BNC", "Licitanet"]
        collected_map: Dict[str, Dict[str, Any]] = {}
        by_portal_stats: Dict[str, int] = {s: 0 for s in active_sources}
        errors: List[str] = []

        target_ufs = ufs if ufs else [None]

        for portal in active_sources:
            try:
                await asyncio.sleep(rate_limit_delay) # Rate-limit preventivo
                portal_items: List[Dict[str, Any]] = []

                if portal.upper() == "PNCP":
                    for uf_val in target_ufs[:3]:
                        items_pncp = await self.fetch_pncp(uf=uf_val, keywords=keywords)
                        portal_items.extend(items_pncp)
                elif "COMPRAS.GOV" in portal.upper() or "COMPRASNET" in portal.upper():
                    portal_items = await self.fetch_compras_gov(keywords=keywords)
                else:
                    portal_items = await self.fetch_state_and_private_portals(portal, keywords=keywords)

                # ``use_fallback`` é mantido só por compatibilidade: não existe mais catálogo fictício.

                for item in portal_items:
                    ext_id = item.get("external_id")
                    if ext_id and ext_id not in collected_map:
                        collected_map[ext_id] = item
                        by_portal_stats[portal] = by_portal_stats.get(portal, 0) + 1

            except Exception as e:
                err_msg = f"Erro ao sincronizar portal {portal}: {str(e)}"
                logger.error(err_msg)
                errors.append(err_msg)

        persisted_count = 0
        if db_session:
            try:
                from server.models import Tender
                from server.seed_data import garantir_fonte_pncp
                # Os certames vêm da API pública do PNCP: pertencem à fonte oficial pncp-source-01
                default_source = garantir_fonte_pncp(db_session)

                for item in collected_map.values():
                    existing = db_session.query(Tender).filter(Tender.external_id == item["external_id"]).first()
                    if not existing:
                        new_tender = Tender(
                            source_id=default_source.id,
                            external_id=item["external_id"],
                            process_number=item.get("process_number"),
                            notice_number=item.get("notice_number"),
                            title=item["title"],
                            object_description=item["object_description"],
                            contracting_entity=item["contracting_entity"],
                            purchasing_unit=item.get("purchasing_unit"),
                            state=item.get("state", "BR"),
                            city=item.get("city", "Não informado"),
                            modality=item.get("modality", "Pregão Eletrônico"),
                            status=item.get("status", "Divulgada no PNCP"),
                            estimated_total_value=item.get("estimated_total_value", 0.0),
                            publication_date=item.get("publication_date"),
                            opening_date=item.get("opening_date"),
                            proposal_deadline=item.get("proposal_deadline"),
                            source_url=item.get("source_url", "https://pncp.gov.br"),
                            fonte_portal=item.get("fonte_portal", "PNCP"),
                            link_edital_oficial=item.get("link_edital_oficial"),
                            url_disputa=item.get("url_disputa"),
                            url_pncp=item.get("url_pncp"),
                            source_system="compras_gov" if item.get("fonte_portal") == "Compras.gov.br" else "pncp"
                        )
                        db_session.add(new_tender)
                        persisted_count += 1
                    else:
                        # Atualiza colunas enriquecidas se estiverem vazias
                        if not existing.fonte_portal or existing.fonte_portal == "PNCP":
                            existing.fonte_portal = item.get("fonte_portal", existing.fonte_portal)
                        if not existing.link_edital_oficial and item.get("link_edital_oficial"):
                            existing.link_edital_oficial = item.get("link_edital_oficial")
                        if not existing.url_disputa and item.get("url_disputa"):
                            existing.url_disputa = item.get("url_disputa")
                        for campo in ("publication_date", "opening_date", "proposal_deadline"):
                            if item.get(campo):
                                setattr(existing, campo, item[campo])
                        persisted_count += 1
                db_session.commit()
            except Exception as dberr:
                db_session.rollback()
                logger.error(f"Erro ao persistir itens sincronizados no banco de dados: {dberr}")
                errors.append(f"DB Error: {str(dberr)}")

        return {
            "status": "success",
            "total_collected": len(collected_map),
            "by_portal": by_portal_stats,
            "persisted_tenders": persisted_count,
            "errors": errors,
            "timestamp": datetime.now().isoformat()
        }

    async def collect_all(
        self,
        uf: Optional[str] = None,
        keyword: Optional[str] = None,
        limit_per_portal: int = 5
    ) -> List[Dict[str, Any]]:
        """Coleta unificada multifonte retornando lista normalizada desduplicada."""
        items: List[Dict[str, Any]] = []
        try:
            pncp_items = await self.fetch_pncp(uf=uf, keywords=keyword, max_results=limit_per_portal)
            items.extend(pncp_items)
        except Exception:
            pass

        try:
            compras_items = await self.fetch_compras_gov(keywords=keyword, max_results=limit_per_portal)
            items.extend(compras_items)
        except Exception:
            pass

        for p_name in ["BNC", "Portal de Compras Públicas", "Licitanet", "BEC-SP"]:
            try:
                portal_res = await self.fetch_state_and_private_portals(p_name, uf=uf, keywords=keyword)
                items.extend(portal_res[:limit_per_portal])
            except Exception:
                pass

        seen = set()
        deduped = []
        for it in items:
            ext = it.get("external_id")
            if ext and ext not in seen:
                seen.add(ext)
                deduped.append({
                    **it,
                    "natureza_objeto": classificar_natureza_certame(it.get("title"), it.get("object_description"))
                })
        return deduped


portal_collector = PortalCollectorService()
sync_pipeline = portal_collector.sync_pipeline
