"""
Módulo de Integrações Externas e Coleta de Oportunidades
"""
from server.integrations.opportunity_collector import (
    sync_procurement_opportunities,
    detect_operational_source,
    generate_comprasnet_link,
    generate_pncp_link
)

__all__ = [
    "sync_procurement_opportunities",
    "detect_operational_source",
    "generate_comprasnet_link",
    "generate_pncp_link"
]
