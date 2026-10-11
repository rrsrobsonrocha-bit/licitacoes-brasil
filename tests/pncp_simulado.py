"""
Respostas simuladas da API pública do PNCP (/v1/contratacoes/publicacao) para os testes.

A suíte roda sem rede; estes registros reproduzem o formato oficial para exercitar a coleta
sem depender de catálogos fictícios dentro da aplicação.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock


def registro_pncp(seq: int, *, federal: bool, modalidade: str = "Pregão Eletrônico", uf: str = "DF",
                  uasg: str = "160016", orgao: str = "Ministério da Defesa",
                  publicado_ha_dias: int = 1, prazo_em_dias: int = 10) -> Dict[str, Any]:
    agora = datetime.now()
    cnpj = "00394452000103" if federal else "46379400000150"
    registro: Dict[str, Any] = {
        "numeroControlePNCP": f"{cnpj}-1-{seq:06d}/2026",
        "numeroCompra": f"{seq}/2026",
        "processo": f"PROC-{seq}/2026",
        "objetoCompra": f"Aquisição de equipamentos de teste número {seq}",
        "modalidadeNome": modalidade,
        "situacaoCompraNome": "Divulgada no PNCP",
        "valorTotalEstimado": 10000.0 + seq,
        "dataPublicacaoPncp": (agora - timedelta(days=publicado_ha_dias)).strftime("%Y-%m-%dT%H:%M:%S"),
        "dataAberturaProposta": (agora + timedelta(days=prazo_em_dias - 1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "dataEncerramentoProposta": (agora + timedelta(days=prazo_em_dias)).strftime("%Y-%m-%dT%H:%M:%S"),
        "orgaoEntidade": {"cnpj": cnpj, "razaoSocial": orgao, "esferaId": "F" if federal else "E"},
        "unidadeOrgao": {"ufSigla": uf, "municipioNome": "Brasília" if uf == "DF" else "Capital",
                         "codigoUnidade": uasg if federal else None, "nomeUnidade": f"Unidade {seq}"},
        "sistemaOrigem": "Compras.gov.br" if federal else "BEC-SP",
        "linkSistemaOrigem": (
            f"https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra={uasg}0500{seq:04d}2026"
            if federal else "https://www.bec.sp.gov.br"
        ),
    }
    return registro


def catalogo_simulado() -> Dict[int, List[Dict[str, Any]]]:
    """Registros por código de modalidade: pregões (federais e estaduais) e dispensas (inclui Marinha/AL)."""
    pregoes = [registro_pncp(i, federal=(i % 2 == 0)) for i in range(1, 61)]
    dispensas = [
        registro_pncp(700 + i, federal=True, modalidade="Dispensa Eletrônica", uf="AL", uasg="783310",
                      orgao="Comando da Marinha - Capitania dos Portos de Alagoas")
        for i in range(1, 6)
    ]
    return {6: pregoes, 8: dispensas, 9: []}


def fetch_tenders_live_simulado(catalogo: Optional[Dict[int, List[Dict[str, Any]]]] = None):
    """Substituto de PNCPConnector.fetch_tenders_live com paginação e filtro por UF."""
    dados = catalogo if catalogo is not None else catalogo_simulado()

    def _fetch(self, data_inicial, data_final, codigo_modalidade=None, uf=None, pagina=1, tamanho_pagina=50):
        registros = list(dados.get(codigo_modalidade, []))
        if uf:
            registros = [r for r in registros if r["unidadeOrgao"]["ufSigla"] == uf.upper()]
        tamanho = max(10, min(tamanho_pagina, 100))
        pagina_registros = registros[(pagina - 1) * tamanho: pagina * tamanho]
        return {"success": True, "data": {"data": pagina_registros}}

    return _fetch


def resposta_httpx_pncp(registros: List[Dict[str, Any]]) -> MagicMock:
    """Resposta HTTP 200 da API do PNCP para coletores assíncronos (httpx)."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": registros}
    return resp
