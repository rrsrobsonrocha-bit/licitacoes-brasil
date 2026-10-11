import os
import sys
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from sqlalchemy.orm import Session
from server.models import Tender, Source
from server.connectors.pncp import PNCPConnector, build_canonical_pncp_url

logger = logging.getLogger(__name__)

def infer_category(title: str, obj: str, default: str = "Outros") -> str:
    """Classifica a categoria de contratação com base nos termos-chave do título e objeto."""
    text = (title + " " + obj).lower()
    if any(k in text for k in ["medicamento", "reagente", "farmac", "vacina", "insulina", "soro", "remedio"]):
        return "Medicamentos"
    if any(k in text for k in ["hospitalar", "curativo", "seringa", "luva", "gaze", "cateter", "ortese", "protese"]):
        return "Materiais hospitalares"
    if any(k in text for k in ["odontol", "escova dental", "resina odont"]):
        return "Materiais odontológicos"
    if any(k in text for k in ["software", "computador", "ti", "datacenter", "servidor", "licen", "ciberseguran", "nuvem"]):
        return "Tecnologia"
    if any(k in text for k in ["equipamento", "aparelho", "viatura", "veiculo", "caminhao", "trator", "maquina"]):
        return "Equipamentos"
    if any(k in text for k in ["serviço", "prestação de serviço", "locação", "manutenção", "vigilância", "limpeza", "consultoria", "apoio"]):
        return "Serviços"
    if any(k in text for k in ["obra", "reforma", "pavimentação", "drenagem", "engenharia", "asfalto", "construção"]):
        return "Obras"
    if any(k in text for k in ["alimentação", "refeição", "gêneros alimentícios", "merenda", "carne", "leite", "nutricional"]):
        return "Alimentação"
    if any(k in text for k in ["limpeza", "asseio", "higienização", "desinfetante", "sabão"]):
        return "Limpeza"
    return default


def _data_pncp(valor: Any) -> Optional[datetime]:
    """Converte uma data/hora do PNCP (horário de Brasília, ISO) em datetime; None se ausente ou inválida."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "").split(".")[0])
    except (TypeError, ValueError):
        return None


def detect_operational_source(record: Dict[str, Any]) -> Tuple[str, Optional[str], bool]:
    """
    Identifica a origem operacional da contratação:
    Retorna: (source_system, uasg_code, sicaf_ready)
    - source_system: 'compras_gov' se vinculado ao Compras.gov.br com UASG e apto ao SICAF, senão 'pncp'
    - uasg_code: código numérico da UASG / Unidade compradora se houver
    - sicaf_ready: True se for do Compras.gov.br
    """
    unidade = record.get("unidadeOrgao", {}) if isinstance(record.get("unidadeOrgao"), dict) else {}
    orgao = record.get("orgaoEntidade", {}) if isinstance(record.get("orgaoEntidade"), dict) else {}

    uasg = (
        record.get("uasg_code")
        or record.get("codigo_unidade")
        or unidade.get("codigoUnidade")
        or record.get("uasg")
        or record.get("codigoUasg")
    )
    if uasg:
        uasg = str(uasg).strip()

    sistema_origem = str(
        record.get("sistema_origem")
        or record.get("sistemaOrigem")
        or ""
    ).lower()

    link_origem = str(
        record.get("link_origem")
        or record.get("linkSistemaOrigem")
        or record.get("source_url")
        or ""
    ).lower()

    esfera = str(
        record.get("administrative_sphere")
        or orgao.get("esferaId")
        or ""
    ).upper()

    is_compras = False

    # A. Link contém domínios oficiais do Compras.gov.br / Comprasnet
    if any(k in link_origem for k in ["comprasnet", "compras.gov.br", "cnetmobile", "comprasgov"]):
        is_compras = True

    # B. Sistema declarado explicitamente como Compras.gov.br ou COMPRASNET
    if any(k in sistema_origem for k in ["compras", "comprasnet", "sisg", "governo federal"]):
        is_compras = True

    # C. Órgão federal com UASG de 5 ou 6 dígitos
    if esfera in ("FEDERAL", "F") and uasg and (len(uasg) in (5, 6) or uasg.isdigit()):
        is_compras = True

    # D. Se for esfera Federal, por padrão legal do SISG utiliza o Compras.gov.br
    if esfera in ("FEDERAL", "F"):
        is_compras = True
        if not uasg:
            match = re.search(r"\b(\d{5,6})\b", record.get("external_id", "") + " " + record.get("process_number", ""))
            if match:
                uasg = match.group(1)

    if is_compras:
        return "compras_gov", uasg, True
    else:
        return "pncp", uasg, False


def detect_executor_portal(record: Dict[str, Any]) -> str:
    """
    Identifica com precisão o portal executor da licitação:
    Compras.gov.br, BEC/SP, Compras MG, Compras RS, Comprasnet BA, SIGA RJ,
    Compras Paraná, BLL Compras, Licitações-e, Portal de Compras Públicas, Licitanet, PNCP Geral.
    """
    explicit = record.get("executor_portal")
    if explicit:
        return explicit

    link = str(record.get("link_origem") or record.get("linkSistemaOrigem") or record.get("official_notice_url") or record.get("source_url") or "").lower()
    sistema = str(record.get("sistema_origem") or record.get("sistemaOrigem") or "").lower()

    if any(k in link or k in sistema for k in ["bec.sp.gov.br", "bec-sp", "bec/sp", "bolsa eletronica de compras"]):
        return "BEC/SP"
    if any(k in link or k in sistema for k in ["bllcompras", "bll compras", "bolsa de licitacoes e leiloes"]):
        return "BLL Compras"
    if any(k in link or k in sistema for k in ["licitacoes-e", "licitações-e"]):
        return "Licitações-e"
    if any(k in link or k in sistema for k in ["portaldecompraspublicas", "portal de compras publicas"]):
        return "Portal de Compras Públicas"
    if any(k in link or k in sistema for k in ["compras.mg.gov.br", "compras mg", "portal de compras mg"]):
        return "Compras MG"
    if any(k in link or k in sistema for k in ["compras.rs.gov.br", "pregaobanrisul", "compras rs", "celic"]):
        return "Compras RS"
    if any(k in link or k in sistema for k in ["comprasnet.ba.gov.br", "compras ba", "comprasnet ba", "compras bahia"]):
        return "Comprasnet BA"
    if any(k in link or k in sistema for k in ["siga.rj.gov.br", "siga rj"]):
        return "SIGA RJ"
    if any(k in link or k in sistema for k in ["comprasparana", "compras parana", "compras paraná"]):
        return "Compras Paraná"
    if any(k in link or k in sistema for k in ["licitanet"]):
        return "Licitanet"
    if any(k in link or k in sistema for k in ["caixa.gov.br"]):
        return "Licitações CAIXA"

    source_sys, _, _ = detect_operational_source(record)
    if source_sys == "compras_gov":
        return "Compras.gov.br"

    return "PNCP Geral"


def generate_comprasnet_link(
    uasg_code: Optional[str] = None,
    notice_number: Optional[str] = None,
    original_link: Optional[str] = None
) -> Optional[str]:
    """Gera link direto para a sala de disputa ou consulta do Compras.gov.br"""
    if original_link and any(k in original_link.lower() for k in ["comprasnet", "compras.gov.br", "cnetmobile"]):
        return original_link

    if uasg_code:
        return f"https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra={uasg_code}"

    return "https://www.gov.br/compras/pt-br/acesso-a-sistemas"


def generate_pncp_link(
    numero_controle_pncp: Optional[str] = None,
    original_link: Optional[str] = None,
    raw_record: Optional[Dict[str, Any]] = None
) -> str:
    """Gera URL pública canônica do edital/termo no PNCP: https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}"""
    if original_link and "pncp.gov.br" in original_link:
        canon = build_canonical_pncp_url(numero_controle_pncp=original_link, raw_record=raw_record)
        if canon != "https://pncp.gov.br/app/editais":
            return canon

    return build_canonical_pncp_url(
        numero_controle_pncp=numero_controle_pncp,
        raw_record=raw_record
    )


def sync_procurement_opportunities(
    db: Session,
    days_back: int = 7,
    uf: Optional[str] = None,
    pages: int = 10,
    limit_per_page: int = 50,
    dry_run: bool = False,
    data_inicial: Optional[str] = None,
    data_final: Optional[str] = None,
    modalidades: Optional[List[int]] = None,
    paginas_por_modalidade: Optional[int] = None,
    reforco_al: bool = True,
    notificar_alertas: bool = True,
    pausa_entre_paginas: float = 0.0,
    ao_progredir: Optional[Callable[[Dict[str, Any]], None]] = None,
    pagina_inicial: Optional[Dict[int, int]] = None,
) -> Dict[str, Any]:
    """
    Pipeline unificado de sincronização em larga escala:
    1. Paginação contínua na API oficial do PNCP (páginas 1 a N, limite_per_page itens por página).
    2. Enriquece a identificação de portais executores (Compras.gov.br, BEC/SP, BLL, Compras RS, MG, BA, RJ, etc.).
    3. Nenhum certame fictício: sem resposta da API, nada é inserido.
    4. Upsert idempotente no SQLite dev_licitacoes.db por external_id (sem duplicidades).
    5. Retorna estatísticas consolidadas: total_coletados, novos_inseridos, atualizados, total_geral_banco.

    Para coletas em lote (auto-warmup), ``data_inicial``/``data_final`` (YYYYMMDD) fixam a janela de
    publicação, ``modalidades`` restringe os códigos consultados e ``paginas_por_modalidade`` pagina
    cada modalidade até esse teto. A paginação segue até a última página informada pelo PNCP
    (``totalPaginas``) ou até uma página vazia; cada página é gravada (commit) assim que chega, e
    ``ao_progredir`` recebe o andamento (modalidade, página, total de páginas, coletados).
    Falhas persistentes da API (após as novas tentativas do conector) ficam em ``erros``.
    ``pagina_inicial`` ({código da modalidade: página}) retoma uma coleta interrompida.
    """
    connector = PNCPConnector()
    now = datetime.utcnow()
    data_final = data_final or now.strftime("%Y%m%d")
    data_inicial = data_inicial or (now - timedelta(days=days_back)).strftime("%Y%m%d")

    records_to_process: List[Dict[str, Any]] = []
    seen_external_ids = set()

    # 1. Paginação contínua multi-modalidade na API oficial do PNCP
    # Modalidades prioritárias: 8 (Dispensa de Licitação / Dispensa Eletrônica), 6 (Pregão Eletrônico), 9 (Inexigibilidade)
    target_modalities = [
        (8, "Dispensa"),
        (6, "Pregão Eletrônico"),
        (9, "Inexigibilidade")
    ]
    if modalidades:
        rotulos = dict(target_modalities)
        target_modalities = [(cod, rotulos[cod]) for cod in modalidades if cod in rotulos]

    # Somente certames realmente retornados pela API oficial do PNCP são gravados:
    # sem rede ou sem resultados, a sincronização não insere nada (catálogo 100% real).
    from server.seed_data import garantir_fonte_pncp
    source = garantir_fonte_pncp(db)

    novos_inseridos = 0
    atualizados = 0
    compras_gov_count = 0
    pncp_count = 0
    dispensas_count = 0
    new_tenders: List[Tender] = []
    erros: List[str] = []
    persistidos = 0

    def persistir_pendentes() -> None:
        """Upsert idempotente (por external_id) dos registros ainda não gravados, com commit imediato."""
        nonlocal novos_inseridos, atualizados, compras_gov_count, pncp_count, dispensas_count, persistidos
        lote = records_to_process[persistidos:]
        persistidos = len(records_to_process)
        if not lote:
            return
        existentes = {
            t.external_id: t
            for t in db.query(Tender).filter(Tender.external_id.in_([o["external_id"] for o in lote]))
        }
        for opp in lote:
            ext_id = opp["external_id"]
            existing = existentes.get(ext_id)

            if opp["source_system"] == "compras_gov":
                compras_gov_count += 1
            else:
                pncp_count += 1

            if "dispensa" in (opp.get("modality") or "").lower():
                dispensas_count += 1

            raw_ref = json.dumps({
                "executor_portal": opp.get("executor_portal", "PNCP Geral"),
                "sistema_origem": opp.get("sistema_origem")
            }, ensure_ascii=False)

            if existing:
                existing.modality = opp["modality"]
                existing.source_system = opp["source_system"]
                existing.uasg_code = opp["uasg_code"]
                existing.sicaf_ready = opp["sicaf_ready"]
                existing.link_comprasnet = opp["link_comprasnet"]
                existing.link_pncp = opp.get("url_pncp") or opp.get("link_pncp")
                existing.url_pncp = opp.get("url_pncp") or opp.get("link_pncp")
                existing.url_disputa = opp.get("url_disputa") or opp.get("official_notice_url") or opp.get("link_comprasnet") or existing.url_pncp
                existing.source_url = existing.url_pncp or existing.link_pncp or existing.source_url
                existing.official_notice_url = opp.get("official_notice_url") or existing.url_disputa
                existing.title = opp["title"]
                existing.object_description = opp["object_description"]
                existing.status = opp["status"]
                existing.estimated_total_value = opp.get("estimated_total_value", existing.estimated_total_value)
                for campo in ("publication_date", "opening_date", "proposal_deadline"):
                    if opp.get(campo):
                        setattr(existing, campo, opp[campo])
                existing.raw_data_reference = raw_ref
                existing.last_synced_at = now
                atualizados += 1
            else:
                new_tender = Tender(
                    source_id=source.id,
                    external_id=ext_id,
                    process_number=opp.get("process_number"),
                    notice_number=opp.get("notice_number"),
                    title=opp["title"],
                    object_description=opp["object_description"],
                    contracting_entity=opp["contracting_entity"],
                    purchasing_unit=opp.get("purchasing_unit"),
                    state=opp.get("state", "DF"),
                    city=opp.get("city", "Brasília"),
                    region=opp.get("region", "Centro-Oeste"),
                    administrative_sphere=opp.get("administrative_sphere", "FEDERAL"),
                    modality=opp.get("modality", "Pregão Eletrônico"),
                    category=opp.get("category", "Geral"),
                    status=opp.get("status", "Divulgada no PNCP"),
                    publication_date=opp.get("publication_date"),
                    opening_date=opp.get("opening_date"),
                    proposal_deadline=opp.get("proposal_deadline"),
                    estimated_total_value=opp.get("estimated_total_value"),
                    source_url=opp.get("url_pncp") or opp.get("link_pncp") or opp.get("source_url", "https://pncp.gov.br"),
                    official_notice_url=opp.get("official_notice_url") or opp.get("url_disputa"),
                    source_system=opp["source_system"],
                    uasg_code=opp["uasg_code"],
                    sicaf_ready=opp["sicaf_ready"],
                    link_comprasnet=opp["link_comprasnet"],
                    link_pncp=opp.get("url_pncp") or opp.get("link_pncp"),
                    url_pncp=opp.get("url_pncp") or opp.get("link_pncp"),
                    url_disputa=opp.get("url_disputa") or opp.get("official_notice_url") or opp.get("link_comprasnet") or opp.get("url_pncp"),
                    raw_data_reference=raw_ref,
                    data_completeness="COMPLETO",
                    created_at=now,
                    last_synced_at=now
                )
                db.add(new_tender)
                db.flush()
                # Os itens reais são obtidos do endpoint /itens do PNCP sob demanda (pncp_compras.sincronizar_itens)
                new_tenders.append(new_tender)
                novos_inseridos += 1
        if dry_run:
            db.flush()
        else:
            db.commit()

    try:
        page_size = max(10, min(limit_per_page, 100))
        if paginas_por_modalidade:
            pages_per_mod = {cod: paginas_por_modalidade for cod, _ in target_modalities}
        elif pages <= 2:
            pages_per_mod = {8: 1, 6: 1, 9: 1}
        else:
            p_disp = max(1, pages // 2)
            p_preg = max(1, pages // 2)
            p_inex = max(1, pages // 4)
            pages_per_mod = {8: p_disp, 6: p_preg, 9: p_inex}

        for mod_code, mod_label in target_modalities:
            max_pages_mod = pages_per_mod.get(mod_code, 1)
            inicio = max(1, (pagina_inicial or {}).get(mod_code, 1))
            for page_num in range(inicio, max_pages_mod + 1):
                if pausa_entre_paginas and page_num > 1:
                    time.sleep(pausa_entre_paginas)  # cortesia com a API pública em coletas longas
                live_result = connector.fetch_tenders_live(
                    data_inicial=data_inicial,
                    data_final=data_final,
                    codigo_modalidade=mod_code,
                    uf=uf,
                    pagina=page_num,
                    tamanho_pagina=page_size
                )

                if live_result.get("success") and live_result.get("data", {}).get("data"):
                    raw_data = live_result["data"]["data"]
                    logger.info(f"[Sync] Modalidade {mod_label} ({mod_code}) - Página {page_num}/{max_pages_mod}: {len(raw_data)} certames.")

                    for r in raw_data:
                        ext_id = r.get("numeroControlePNCP") or f"pncp-{r.get('id', '')}"
                        if not ext_id or ext_id in seen_external_ids:
                            continue
                        seen_external_ids.add(ext_id)

                        source_sys, uasg, sicaf = detect_operational_source(r)
                        portal_name = detect_executor_portal(r)

                        # Datas oficiais informadas pelo PNCP; ausentes ficam vazias (nunca estimadas)
                        pub_date = _data_pncp(r.get("dataPublicacaoPncp"))
                        open_date = _data_pncp(r.get("dataAberturaProposta"))
                        deadline = _data_pncp(r.get("dataEncerramentoProposta"))

                        unidade = r.get("unidadeOrgao", {}) if isinstance(r.get("unidadeOrgao"), dict) else {}
                        orgao = r.get("orgaoEntidade", {}) if isinstance(r.get("orgaoEntidade"), dict) else {}

                        title_str = (r.get("objetoCompra") or "Contratação Pública")[:500]
                        obj_desc = r.get("objetoCompra") or "Sem descrição detalhada"
                        inferred_cat = infer_category(title_str, obj_desc)

                        # Normalização rigorosa e consistente da modalidade
                        raw_mod = str(r.get("modalidadeNome") or "").strip()
                        raw_mod_lower = raw_mod.lower()
                        if mod_code == 8 or "dispensa" in raw_mod_lower:
                            if "eletr" in raw_mod_lower or sicaf or "comprasnet" in str(r).lower() or "cnet" in str(r).lower():
                                modality_clean = "Dispensa Eletrônica"
                            elif "licita" in raw_mod_lower:
                                modality_clean = "Dispensa de Licitação"
                            else:
                                modality_clean = "Dispensa Eletrônica"
                        elif mod_code == 9 or "inexigib" in raw_mod_lower:
                            modality_clean = "Inexigibilidade"
                        elif mod_code == 6 or "preg" in raw_mod_lower:
                            modality_clean = "Pregão Eletrônico"
                        elif "concorr" in raw_mod_lower:
                            modality_clean = "Concorrência"
                        else:
                            modality_clean = raw_mod or "Pregão Eletrônico"

                        pncp_url = generate_pncp_link(ext_id, r.get("linkSistemaOrigem"), raw_record=r)
                        cnet_url = generate_comprasnet_link(uasg, r.get("numeroCompra"), r.get("linkSistemaOrigem")) if sicaf else None
                        dispute_url = r.get("linkSistemaOrigem") or cnet_url or pncp_url

                        records_to_process.append({
                            "external_id": ext_id,
                            "process_number": r.get("processo"),
                            "notice_number": r.get("numeroCompra"),
                            "title": title_str,
                            "object_description": obj_desc,
                            "contracting_entity": orgao.get("razaoSocial", "Órgão Não Informado"),
                            "purchasing_unit": unidade.get("nomeUnidade"),
                            "state": unidade.get("ufSigla", uf or "DF"),
                            "city": unidade.get("municipioNome", "Não informado"),
                            "administrative_sphere": orgao.get("esferaId", "FEDERAL"),
                            "modality": modality_clean,
                            "category": inferred_cat,
                            "status": r.get("situacaoCompraNome", "Divulgada no PNCP"),
                            "estimated_total_value": r.get("valorTotalEstimado") or r.get("valorTotalHomologado"),
                            "publication_date": pub_date,
                            "opening_date": open_date,
                            "proposal_deadline": deadline,
                            "source_system": source_sys,
                            "uasg_code": uasg,
                            "sicaf_ready": sicaf,
                            "url_disputa": dispute_url,
                            "url_pncp": pncp_url,
                            "link_comprasnet": cnet_url,
                            "link_pncp": pncp_url,
                            "source_url": pncp_url,
                            "official_notice_url": r.get("linkSistemaOrigem") or dispute_url,
                            "executor_portal": portal_name
                        })

                    persistir_pendentes()
                    total_paginas = int(live_result["data"].get("totalPaginas") or 0)
                    ultima = len(raw_data) < page_size or bool(total_paginas and page_num >= total_paginas)
                    if ao_progredir:
                        ao_progredir({
                            "codigo_modalidade": mod_code,
                            "modalidade": mod_label,
                            "pagina": page_num,
                            "total_paginas": total_paginas,
                            "concluido": ultima,
                            "coletados": len(records_to_process),
                            "novos": novos_inseridos,
                        })
                    if ultima:
                        break
                elif live_result.get("success"):
                    # Página vazia: não há mais registros nesta modalidade
                    if ao_progredir:
                        ao_progredir({
                            "codigo_modalidade": mod_code,
                            "modalidade": mod_label,
                            "pagina": page_num - 1,
                            "total_paginas": int((live_result.get("data") or {}).get("totalPaginas") or 0),
                            "concluido": True,
                            "coletados": len(records_to_process),
                            "novos": novos_inseridos,
                        })
                    break
                else:
                    # Falha persistente mesmo após as novas tentativas do conector (ex.: 429 contínuo)
                    erros.append(f"{mod_label} página {page_num}: {str(live_result.get('error', ''))[:120]}")
                    logger.warning("[Sync] %s", erros[-1])
                    break

        # Coleta de reforço para Alagoas (AL) se uf não for restritivo para outro estado
        if reforco_al and (not uf or uf.upper() == "AL"):
            al_result = connector.fetch_tenders_live(
                data_inicial=data_inicial,
                data_final=data_final,
                codigo_modalidade=8,
                uf="AL",
                pagina=1,
                tamanho_pagina=page_size
            )
            if al_result.get("success") and al_result.get("data", {}).get("data"):
                for r in al_result["data"]["data"]:
                    ext_id = r.get("numeroControlePNCP") or f"pncp-{r.get('id', '')}"
                    if not ext_id or ext_id in seen_external_ids:
                        continue
                    seen_external_ids.add(ext_id)
                    source_sys, uasg, sicaf = detect_operational_source(r)
                    portal_name = detect_executor_portal(r)
                    title_str = (r.get("objetoCompra") or "Contratação Pública")[:500]
                    obj_desc = r.get("objetoCompra") or "Sem descrição detalhada"
                    unidade = r.get("unidadeOrgao", {}) if isinstance(r.get("unidadeOrgao"), dict) else {}
                    orgao = r.get("orgaoEntidade", {}) if isinstance(r.get("orgaoEntidade"), dict) else {}
                    pncp_url = generate_pncp_link(ext_id, r.get("linkSistemaOrigem"), raw_record=r)
                    cnet_url = generate_comprasnet_link(uasg, r.get("numeroCompra"), r.get("linkSistemaOrigem")) if sicaf else None
                    dispute_url = r.get("linkSistemaOrigem") or cnet_url or pncp_url

                    records_to_process.append({
                        "external_id": ext_id,
                        "process_number": r.get("processo"),
                        "notice_number": r.get("numeroCompra"),
                        "title": title_str,
                        "object_description": obj_desc,
                        "contracting_entity": orgao.get("razaoSocial", "Órgão Não Informado"),
                        "purchasing_unit": unidade.get("nomeUnidade"),
                        "state": "AL",
                        "city": unidade.get("municipioNome", "Maceió"),
                        "administrative_sphere": orgao.get("esferaId", "FEDERAL"),
                        "modality": "Dispensa Eletrônica",
                        "category": infer_category(title_str, obj_desc),
                        "status": r.get("situacaoCompraNome", "Divulgada no PNCP"),
                        "estimated_total_value": r.get("valorTotalEstimado") or r.get("valorTotalHomologado"),
                        "publication_date": _data_pncp(r.get("dataPublicacaoPncp")),
                        "opening_date": _data_pncp(r.get("dataAberturaProposta")),
                        "proposal_deadline": _data_pncp(r.get("dataEncerramentoProposta")),
                        "source_system": source_sys,
                        "uasg_code": uasg,
                        "sicaf_ready": sicaf,
                        "url_disputa": dispute_url,
                        "url_pncp": pncp_url,
                        "link_comprasnet": cnet_url,
                        "link_pncp": pncp_url,
                        "source_url": pncp_url,
                        "official_notice_url": r.get("linkSistemaOrigem") or dispute_url,
                        "executor_portal": portal_name
                    })

    except Exception as exc:
        # Páginas já gravadas permanecem; o lote em andamento é descartado e a coleta segue no próximo ciclo
        db.rollback()
        erros.append(f"Erro na consulta contínua ao PNCP: {exc}")
        logger.warning(f"[Sync] Erro na consulta contínua ao PNCP: {exc}")

    persistir_pendentes()

    if not dry_run:
        db.commit()
        if new_tenders and notificar_alertas:
            try:
                from server.services.alert_service import check_and_deliver_tender_alerts
                for nt in new_tenders:
                    check_and_deliver_tender_alerts(db, nt)
            except Exception as alert_err:
                logger.warning(f"[Sync] Erro no processamento de alertas automáticos: {alert_err}")
    else:
        db.rollback()
        logger.info("[Sync] Execução DRY-RUN concluída com sucesso (banco preservado sem persistência).")

    total_geral_banco = db.query(Tender).count()

    return {
        "status": "success" if not dry_run else "dry_run_success",
        "dry_run": dry_run,
        "message": f"Sincronização concluída ({'DRY-RUN' if dry_run else 'PERSISTIDO'}): {novos_inseridos} novos certames, {atualizados} atualizados ({dispensas_count} dispensas).",
        "synced_at": now.isoformat(),
        "total_processados": len(records_to_process),
        "total_coletados": len(records_to_process),
        "novos_inseridos": novos_inseridos,
        "atualizados": atualizados,
        "compras_gov_count": compras_gov_count,
        "pncp_count": pncp_count,
        "dispensas_count": dispensas_count,
        "total_geral_banco": total_geral_banco,
        "erros": erros
    }

if __name__ == "__main__":
    from server.database import SessionLocal
    with SessionLocal() as _db_session:
        _result = sync_procurement_opportunities(_db_session, pages=2, limit_per_page=25, dry_run=True)
        print(json.dumps(_result, indent=2, ensure_ascii=False))
