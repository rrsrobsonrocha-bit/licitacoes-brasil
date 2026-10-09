from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseConnector(ABC):
    """Classe base para conectores de fontes de licitação públicas"""
    
    def __init__(self, source_id: str, name: str, base_url: str, official_domain: str):
        self.source_id = source_id
        self.name = name
        self.base_url = base_url
        self.official_domain = official_domain

    @abstractmethod
    def fetch_tenders(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Busca contratações da fonte oficial"""
        pass

    @abstractmethod
    def get_official_search_url(self, query: str = "") -> str:
        """Retorna link direto para a consulta pública oficial do portal"""
        pass
