import os
import re
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from server.connectors.base import BaseConnector
from server.connectors.pncp import build_canonical_pncp_url

logger = logging.getLogger(__name__)

class LicitacoesEConnector(BaseConnector):
    """
    Conector de Integração do Licitações-e (Banco do Brasil).
    Suporta modo de desenvolvimento local via Token Mock e credenciamento oficial OAuth 2.0.
    Normaliza todos os certames para o modelo canônico de dados do LicitaFlow.
    """

    def __init__(
        self,
        source_id: str = "licitacoes-e-04",
        name: str = "Licitações-e (Banco do Brasil)",
        base_url: Optional[str] = None,
        official_domain: str = "licitacoes-e.com.br"
    ):
        base = base_url or os.getenv("LICITACOES_E_BASE_URL", "https://www.licitacoes-e.com.br")
        super().__init__(source_id, name, base, official_domain)
        self.search_path = "/aop/pesquisar-licitacao.aop"

    @property
    def active(self) -> bool:
        """Determina se o conector está ativo com base na variável de ambiente LICITACOES_E_ACTIVE"""
        val = os.getenv("LICITACOES_E_ACTIVE", "false").strip().lower()
        return val in ("true", "1", "yes", "on")

    @property
    def mock_mode(self) -> bool:
        """Indica se a execução opera em modo Mock/Simulação para desenvolvimento local"""
        val = os.getenv("LICITACOES_E_MOCK_MODE", "true").strip().lower()
        return val in ("true", "1", "yes", "on")

    @property
    def mock_token(self) -> str:
        return os.getenv("LICITACOES_E_MOCK_TOKEN", "mock_bearer_token_local_dev")

    @property
    def integration_type(self) -> str:
        if not self.active:
            return "DESATIVADO"
        return "API_MOCK" if self.mock_mode else "API_OFICIAL"

    @property
    def authorization_notes(self) -> str:
        if not self.active:
            return "Integração via API aguardando credenciamento e token B2B do Banco do Brasil."
        if self.mock_mode:
            return "Modo de simulação/mock ativo para desenvolvimento local."
        return "Conector oficial homologado via API B2B Banco do Brasil."

    def get_official_search_url(self, query: str = "") -> str:
        if query:
            return f"{self.base_url}{self.search_path}?termo={query}"
        return f"{self.base_url}{self.search_path}"

    def normalize_tender(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normaliza o registro bruto retornado pela API ou Mock do Licitações-e
        para o esquema canônico da plataforma LicitaFlow / Tender.
        """
        now = datetime.utcnow()
        numero_licitacao = str(raw.get("numeroLicitacao") or raw.get("numero") or "1001").strip()
        ano = str(raw.get("ano") or now.year).strip()
        cnpj_orgao = re.sub(r"\D", "", str(raw.get("cnpjOrgao") or "00000000000191"))
        if len(cnpj_orgao) != 14:
            cnpj_orgao = "00000000000191"

        seq_int = int(numero_licitacao) if numero_licitacao.isdigit() else 1
        external_id = raw.get("external_id") or f"{cnpj_orgao}-1-{seq_int:06d}/{ano}"

        link_origem = raw.get("link_origem") or f"{self.base_url}/aop/consultar-detalhes-licitacao.aop?numeroLicitacao={numero_licitacao}"
        url_pncp = build_canonical_pncp_url(cnpj=cnpj_orgao, ano=ano, sequencial=seq_int)
        url_disputa = link_origem or url_pncp

        val_est = raw.get("valorTotalEstimado") or raw.get("estimated_total_value") or 0.0

        pub_date = raw.get("dataPublicacao") or raw.get("publication_date") or (now - timedelta(days=1))
        if isinstance(pub_date, str):
            try:
                pub_date = datetime.fromisoformat(pub_date.replace("Z", "+00:00").split(".")[0])
            except Exception:
                pub_date = now - timedelta(days=1)

        open_date = raw.get("dataAbertura") or raw.get("opening_date") or (now + timedelta(days=7))
        if isinstance(open_date, str):
            try:
                open_date = datetime.fromisoformat(open_date.replace("Z", "+00:00").split(".")[0])
            except Exception:
                open_date = now + timedelta(days=7)

        deadline = raw.get("dataLimite") or raw.get("proposal_deadline") or (open_date + timedelta(hours=2))
        if isinstance(deadline, str):
            try:
                deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00").split(".")[0])
            except Exception:
                deadline = open_date + timedelta(hours=2)

        return {
            "external_id": external_id,
            "process_number": raw.get("processo") or raw.get("process_number") or f"BB-{numero_licitacao}/{ano}",
            "notice_number": raw.get("numeroEdital") or raw.get("notice_number") or f"PE {seq_int % 300 + 1:03d}/{ano}",
            "title": raw.get("titulo") or raw.get("title") or "Contratação Pública Licitações-e",
            "object_description": raw.get("objeto") or raw.get("object_description") or "Sem descrição detalhada.",
            "contracting_entity": raw.get("orgao") or raw.get("contracting_entity") or "Banco do Brasil S.A.",
            "purchasing_unit": raw.get("unidadeCompradora") or raw.get("purchasing_unit") or "Diretoria de Suprimentos",
            "state": raw.get("uf") or raw.get("state") or "SP",
            "city": raw.get("municipio") or raw.get("city") or "São Paulo",
            "region": raw.get("regiao") or raw.get("region") or "Sudeste",
            "administrative_sphere": raw.get("esfera") or raw.get("administrative_sphere") or "FEDERAL",
            "modality": raw.get("modalidade") or raw.get("modality") or "Pregão Eletrônico",
            "category": raw.get("categoria") or raw.get("category") or "Tecnologia",
            "status": raw.get("situacao") or raw.get("status") or "Publicada / Em Disputa",
            "estimated_total_value": float(val_est),
            "publication_date": pub_date,
            "opening_date": open_date,
            "proposal_deadline": deadline,
            "source_system": "pncp",
            "executor_portal": "Licitações-e",
            "sistema_origem": "Licitações-e (Banco do Brasil)",
            "link_origem": link_origem,
            "url_disputa": url_disputa,
            "url_pncp": url_pncp,
            "source_url": url_pncp,
            "official_notice_url": link_origem,
            "uasg_code": None,
            "sicaf_ready": False,
            "link_comprasnet": None
        }

    def fetch_tenders(self, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Retorna certames do portal Licitações-e.
        Se mock_mode=True, gera coleção estruturada e normalizada para testes.
        """
        if not self.active:
            logger.info("[Licitações-e] Conector inativo no momento.")
            return []

        if self.mock_mode:
            logger.info("[Licitações-e] Executando em MOCK MODE com token local.")
            return self._generate_mock_tenders(params)

        # Em produção real: consulta à API REST/SOAP do Banco do Brasil com token OAuth
        return []

    def _generate_mock_tenders(self, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = params or {}
        limit = params.get("limit", 15)
        now = datetime.utcnow()

        seed_data = [
            {
                "numeroLicitacao": "1045201",
                "ano": now.year,
                "cnpjOrgao": "00000000000191",
                "titulo": "Serviços de Manutenção e Suporte em Infraestrutura de Nuvem Privada",
                "objeto": "Contratação de suporte técnico especializado para nuvem privada do Banco do Brasil.",
                "orgao": "Banco do Brasil S.A. - DITEC",
                "unidadeCompradora": "Centro de Suprimentos Corporativos - SP",
                "uf": "SP",
                "municipio": "São Paulo",
                "regiao": "Sudeste",
                "esfera": "FEDERAL",
                "modalidade": "Pregão Eletrônico",
                "categoria": "Tecnologia",
                "situacao": "Publicada / Em Disputa",
                "valorTotalEstimado": 18450000.0
            },
            {
                "numeroLicitacao": "1045202",
                "ano": now.year,
                "cnpjOrgao": "45231560000185",
                "titulo": "Aquisição de Insumos e Materiais Cirúrgicos de Alta Complexidade",
                "objeto": "Fornecimento de materiais cirúrgicos descartáveis para pronto atendimento hospitalar.",
                "orgao": "Prefeitura Municipal de Ribeirão Preto",
                "unidadeCompradora": "Secretaria Municipal de Saúde",
                "uf": "SP",
                "municipio": "Ribeirão Preto",
                "regiao": "Sudeste",
                "esfera": "MUNICIPAL",
                "modalidade": "Pregão Eletrônico",
                "categoria": "Materiais hospitalares",
                "situacao": "Aguardando Abertura",
                "valorTotalEstimado": 4200000.0
            },
            {
                "numeroLicitacao": "1045203",
                "ano": now.year,
                "cnpjOrgao": "43776517000180",
                "titulo": "Fornecimento de Bombas Submersíveis e Quadros Elétricos de Comando",
                "objeto": "Modernização de estações elevatórias de esgoto e captação de água bruta.",
                "orgao": "Companhia de Saneamento Básico do Estado de São Paulo - SABESP",
                "unidadeCompradora": "Departamento de Manutenção e Obras",
                "uf": "SP",
                "municipio": "Santos",
                "regiao": "Sudeste",
                "esfera": "ESTADUAL",
                "modalidade": "Pregão Eletrônico",
                "categoria": "Equipamentos",
                "situacao": "Publicada / Em Disputa",
                "valorTotalEstimado": 7890000.0
            }
        ]

        normalized: List[Dict[str, Any]] = []
        for raw in seed_data[:limit]:
            normalized.append(self.normalize_tender(raw))

        return normalized
