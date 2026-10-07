from typing import Dict, List, Any, Optional
from server.connectors.base import BaseConnector
from server.connectors.pncp import PNCPConnector
from server.connectors.licitacoes_e import LicitacoesEConnector

class GenericOfficialPortalConnector(BaseConnector):
    """
    Conector padronizado para portais estaduais, municipais e privados autorizados.
    Garante compliance: não realiza scraping agressivo, respeita termos de uso e
    disponibiliza links oficiais, metadados verificados e importação controlada.
    """
    def __init__(
        self,
        source_id: str,
        name: str,
        base_url: str,
        official_domain: str,
        search_path: str,
        integration_type: str = "API_OFICIAL",
        active: bool = True,
        authorization_notes: str = ""
    ):
        super().__init__(source_id, name, base_url, official_domain)
        self.search_path = search_path
        self.integration_type = integration_type
        self.active = active
        self.authorization_notes = authorization_notes

    def fetch_tenders(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Para portais sem API aberta homologada ou sem autorização explícita,
        # opera via links oficiais e importações estruturadas de arquivos autorizados.
        return []

    def get_official_search_url(self, query: str = "") -> str:
        sep = "&" if "?" in self.search_path else "?"
        if query:
            return f"{self.base_url}{self.search_path}{sep}termo={query}"
        return f"{self.base_url}{self.search_path}"


# Registro Central de Fontes de Contratações Públicas
SOURCE_CATALOG = [
    PNCPConnector(),
    GenericOfficialPortalConnector(
        source_id="comprasgov-source-02",
        name="Compras.gov.br (Antigo Comprasnet)",
        base_url="https://comprasgovernamentais.gov.br",
        official_domain="gov.br",
        search_path="/consultas/quadro-avisos",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Portal oficial do Governo Federal gerido pelo MGI/SEGES."
    ),
    GenericOfficialPortalConnector(
        source_id="portalcompraspublicas-03",
        name="Portal de Compras Públicas",
        base_url="https://www.portaldecompraspublicas.com.br",
        official_domain="portaldecompraspublicas.com.br",
        search_path="/processos",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Plataforma privada autorizada para licitações municipais e estaduais."
    ),
    LicitacoesEConnector(),
    GenericOfficialPortalConnector(
        source_id="licitacoes-caixa-05",
        name="Licitações CAIXA",
        base_url="https://www.caixa.gov.br",
        official_domain="caixa.gov.br",
        search_path="/poder-publico/compras-governamentais/licitacoes",
        integration_type="EXPORTACAO_ARQUIVO",
        active=True,
        authorization_notes="Consulta via extrato oficial de editais da Caixa Econômica Federal."
    ),
    GenericOfficialPortalConnector(
        source_id="compras-rs-06",
        name="Compras RS (Governo do Rio Grande do Sul)",
        base_url="https://compras.rs.gov.br",
        official_domain="rs.gov.br",
        search_path="/editais/pesquisa",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Portal oficial de compras públicas do Estado do Rio Grande do Sul (CELIC)."
    ),
    GenericOfficialPortalConnector(
        source_id="compras-mg-07",
        name="Compras Minas Gerais (Portal de Compras MG)",
        base_url="https://compras.mg.gov.br",
        official_domain="mg.gov.br",
        search_path="/processos-de-compras",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Sistema integrado de compras públicas de Minas Gerais (SEPLAG-MG)."
    ),
    GenericOfficialPortalConnector(
        source_id="bll-compras-08",
        name="BLL Compras (Bolsa de Licitações e Leilões)",
        base_url="https://bllcompras.com",
        official_domain="bllcompras.com",
        search_path="/Process/ProcessSearchPublic",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Plataforma eletrônica para pregões e dispensas no âmbito nacional."
    ),
    GenericOfficialPortalConnector(
        source_id="licitanet-09",
        name="Licitanet Licitações On-line",
        base_url="https://licitanet.com.br",
        official_domain="licitanet.com.br",
        search_path="/pesquisa-editais",
        integration_type="API_OFICIAL",
        active=True,
        authorization_notes="Portal eletrônico homologado para contratações públicas."
    ),
    GenericOfficialPortalConnector(
        source_id="compras-ba-10",
        name="Compras Bahia (Governo do Estado da Bahia)",
        base_url="https://www.comprasnet.ba.gov.br",
        official_domain="ba.gov.br",
        search_path="/consulta-editais",
        integration_type="EXPORTACAO_ARQUIVO",
        active=True,
        authorization_notes="Portal de compras do Estado da Bahia (SAEB)."
    )
]

def get_connector(source_id: str) -> Optional[BaseConnector]:
    for connector in SOURCE_CATALOG:
        if connector.source_id == source_id:
            return connector
    return None

def list_all_sources() -> List[Dict[str, Any]]:
    return [
        {
            "id": c.source_id,
            "name": c.name,
            "base_url": c.base_url,
            "official_domain": c.official_domain,
            "integration_type": getattr(c, "integration_type", "API_OFICIAL"),
            "active": getattr(c, "active", True),
            "authorization_notes": getattr(c, "authorization_notes", "")
        }
        for c in SOURCE_CATALOG
    ]
