"""
Dossiê do Fornecedor (Licitações Brasil): dados públicos de um CNPJ reunidos de fontes oficiais.

- Receita Federal (via BrasilAPI): cadastro, CNAEs e quadro societário (QSA).
- PNCP (busca pública): contratos firmados pelo fornecedor, valor acumulado e órgãos compradores.
- Portal da Transparência (CEIS/CNEP): sanções, somente com PORTAL_TRANSPARENCIA_API_KEY.

Nada é inventado: fonte fora do ar ou não configurada aparece como "não verificado".
Os dossiês ficam em cache no banco por DOSSIE_CACHE_HORAS (24 h).
"""
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from server.models import DossieFornecedor, hoje_sao_paulo

BRASILAPI_CNPJ_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
PNCP_BUSCA_URL = "https://pncp.gov.br/api/search/"
PNCP_APP_URL = "https://pncp.gov.br/app"
TRANSPARENCIA_URL = "https://api.portaldatransparencia.gov.br/api-de-dados/{cadastro}"
TRANSPARENCIA_CONSULTA_URL = "https://portaldatransparencia.gov.br/sancoes/consulta?cpfCnpj={cnpj}"

DOSSIE_CACHE_HORAS = int(os.getenv("DOSSIE_CACHE_HORAS", "24"))
CONTRATOS_PAGINAS = 3
CONTRATOS_POR_PAGINA = 100
TIMEOUT = 20.0
TENTATIVAS = 3
ESPERA_ENTRE_TENTATIVAS = 1.0  # segundos (1 s, depois 2 s)
CABECALHOS = {"User-Agent": "LicitacoesBrasil/1.0 (+https://licitacoes-brasil.onrender.com)",
              "Accept": "application/json"}


class CnpjInvalidoError(ValueError):
    """CNPJ com formato ou dígitos verificadores inválidos."""


class CnpjNaoEncontradoError(LookupError):
    """CNPJ válido, mas inexistente na base da Receita."""


class FonteIndisponivelError(RuntimeError):
    """A fonte pública não respondeu (fora do ar, limite de requisições, rede)."""


# ------------------------------------------------------------------------------
# CNPJ
# ------------------------------------------------------------------------------
def somente_digitos(valor: Optional[str]) -> str:
    return re.sub(r"\D", "", valor or "")


def cnpj_valido(cnpj: str) -> bool:
    digitos = somente_digitos(cnpj)
    if len(digitos) != 14 or digitos == digitos[0] * 14:
        return False

    def _dv(base: str) -> str:
        pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2] if len(base) == 12 else [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        resto = sum(int(d) * p for d, p in zip(base, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    return digitos[12] == _dv(digitos[:12]) and digitos[13] == _dv(digitos[:13])


def normalizar_cnpj(cnpj: str) -> str:
    """Somente os 14 dígitos; CnpjInvalidoError se o CNPJ não for válido."""
    digitos = somente_digitos(cnpj)
    if not cnpj_valido(digitos):
        raise CnpjInvalidoError("CNPJ inválido: confira os 14 dígitos.")
    return digitos


def formatar_cnpj(cnpj: str) -> str:
    d = somente_digitos(cnpj)
    if len(d) != 14:
        return cnpj or ""
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


# ------------------------------------------------------------------------------
# HTTP (ponto único: os testes substituem esta função)
# ------------------------------------------------------------------------------
def requisitar_json(url: str, params: Optional[Dict[str, Any]] = None,
                    headers: Optional[Dict[str, str]] = None) -> Any:
    """
    GET que devolve o JSON; None em 404; FonteIndisponivelError em falha de rede ou HTTP.
    Falhas transitórias (conexão derrubada, 429, 5xx: o PNCP fecha cerca de metade das conexões
    sem resposta) são repetidas até TENTATIVAS vezes.
    """
    host = httpx.URL(url).host
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = httpx.get(url, params=params, headers={**CABECALHOS, **(headers or {})},
                                 timeout=TIMEOUT, follow_redirects=True)
        except (httpx.HTTPError, OSError) as exc:
            if tentativa < TENTATIVAS:
                time.sleep(ESPERA_ENTRE_TENTATIVAS * tentativa)
                continue
            raise FonteIndisponivelError(f"Sem resposta de {host}: {exc.__class__.__name__}") from exc
        if (resposta.status_code == 429 or resposta.status_code >= 500) and tentativa < TENTATIVAS:
            time.sleep(ESPERA_ENTRE_TENTATIVAS * tentativa)
            continue
        break
    if resposta.status_code == 404:
        return None
    if resposta.status_code >= 400:
        raise FonteIndisponivelError(f"{host} respondeu HTTP {resposta.status_code}")
    try:
        return resposta.json()
    except ValueError as exc:
        raise FonteIndisponivelError(f"{host} não devolveu JSON") from exc


# ------------------------------------------------------------------------------
# Receita Federal (BrasilAPI)
# ------------------------------------------------------------------------------
def _data(valor: Optional[str]) -> Optional[str]:
    try:
        return datetime.strptime((valor or "")[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def consultar_receita(cnpj: str) -> Dict[str, Any]:
    """Cadastro do CNPJ na Receita Federal (via BrasilAPI), normalizado."""
    cnpj = normalizar_cnpj(cnpj)
    bruto = requisitar_json(BRASILAPI_CNPJ_URL.format(cnpj=cnpj))
    if not bruto:
        raise CnpjNaoEncontradoError("CNPJ não encontrado na base da Receita Federal.")
    complemento = (bruto.get("complemento") or "").strip()
    return {
        "cnpj": cnpj,
        "cnpj_formatado": formatar_cnpj(cnpj),
        "razao_social": bruto.get("razao_social") or "",
        "nome_fantasia": bruto.get("nome_fantasia") or "",
        "situacao": (bruto.get("descricao_situacao_cadastral") or "").upper(),
        "data_situacao": _data(bruto.get("data_situacao_cadastral")),
        "motivo_situacao": bruto.get("descricao_motivo_situacao_cadastral") or "",
        "data_abertura": _data(bruto.get("data_inicio_atividade")),
        "capital_social": float(bruto.get("capital_social") or 0),
        "porte": bruto.get("porte") or bruto.get("descricao_porte") or "",
        "natureza_juridica": bruto.get("natureza_juridica") or "",
        "opcao_simples": bruto.get("opcao_pelo_simples"),
        "opcao_mei": bruto.get("opcao_pelo_mei"),
        "matriz": (bruto.get("descricao_identificador_matriz_filial") or "").upper() == "MATRIZ",
        "email": (bruto.get("email") or "").lower(),
        "telefone": bruto.get("ddd_telefone_1") or "",
        "endereco": {
            "logradouro": " ".join(p for p in (bruto.get("descricao_tipo_de_logradouro"), bruto.get("logradouro")) if p),
            "numero": bruto.get("numero") or "",
            "complemento": complemento,
            "bairro": bruto.get("bairro") or "",
            "municipio": bruto.get("municipio") or "",
            "uf": bruto.get("uf") or "",
            "cep": somente_digitos(str(bruto.get("cep") or "")),
        },
        "cnae_principal": {"codigo": str(bruto.get("cnae_fiscal") or ""),
                           "descricao": bruto.get("cnae_fiscal_descricao") or ""},
        "cnaes_secundarios": [{"codigo": str(c.get("codigo") or ""), "descricao": c.get("descricao") or ""}
                              for c in (bruto.get("cnaes_secundarios") or []) if c.get("codigo")],
        "qsa": [{
            "nome": s.get("nome_socio") or "",
            "qualificacao": s.get("qualificacao_socio") or "",
            "data_entrada": _data(s.get("data_entrada_sociedade")),
            "faixa_etaria": s.get("faixa_etaria") or "",
            "documento": s.get("cnpj_cpf_do_socio") or "",  # a Receita já devolve o CPF mascarado
        } for s in (bruto.get("qsa") or [])],
        "fonte": "Receita Federal (via BrasilAPI)",
    }


def tempo_de_fundacao(data_abertura: Optional[str], hoje: Optional[date] = None) -> Optional[Dict[str, int]]:
    if not data_abertura:
        return None
    inicio = date.fromisoformat(data_abertura)
    ref = hoje or hoje_sao_paulo()
    meses = (ref.year - inicio.year) * 12 + (ref.month - inicio.month) - (1 if ref.day < inicio.day else 0)
    meses = max(0, meses)
    return {"anos": meses // 12, "meses": meses % 12, "total_meses": meses}


# ------------------------------------------------------------------------------
# PNCP: contratos do fornecedor
# ------------------------------------------------------------------------------
def consultar_contratos_pncp(cnpj: str) -> Dict[str, Any]:
    """
    Contratos publicados no PNCP em que o CNPJ é o fornecedor (campo ``fornecedor_ni``),
    dos mais recentes para os mais antigos, até CONTRATOS_PAGINAS páginas.
    """
    cnpj = normalizar_cnpj(cnpj)
    contratos: List[Dict[str, Any]] = []
    total_informado = 0
    lidos = 0
    for pagina in range(1, CONTRATOS_PAGINAS + 1):
        resposta = requisitar_json(PNCP_BUSCA_URL, params={
            "q": cnpj, "tipos_documento": "contrato", "ordenacao": "-data",
            "pagina": pagina, "tam_pagina": CONTRATOS_POR_PAGINA,
        }) or {}
        itens = resposta.get("items") or []
        total_informado = int(resposta.get("total") or 0)
        lidos += len(itens)
        contratos.extend(i for i in itens if somente_digitos(i.get("fornecedor_ni")) == cnpj and not i.get("cancelado"))
        if len(itens) < CONTRATOS_POR_PAGINA or lidos >= total_informado:
            break

    por_orgao: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"contratos": 0, "valor": 0.0})
    valor_total = 0.0
    for c in contratos:
        valor = float(c.get("valor_global") or 0)
        valor_total += valor
        chave = c.get("orgao_cnpj") or c.get("orgao_nome") or "?"
        orgao = por_orgao[chave]
        orgao.update(nome=c.get("orgao_nome") or "", cnpj=c.get("orgao_cnpj") or "", uf=c.get("uf") or "")
        orgao["contratos"] += 1
        orgao["valor"] += valor

    recentes = [{
        "titulo": c.get("title") or "",
        "objeto": (c.get("description") or "")[:400],
        "orgao": c.get("orgao_nome") or "",
        "uf": c.get("uf") or "",
        "municipio": c.get("municipio_nome") or "",
        "modalidade": c.get("modalidade_licitacao_nome") or "",
        "valor": float(c.get("valor_global") or 0),
        "data_assinatura": _data(c.get("data_assinatura")),
        "vigencia_fim": _data(c.get("data_fim_vigencia")),
        "url": f"{PNCP_APP_URL}{c['item_url']}" if c.get("item_url") else None,
    } for c in contratos[:10]]

    datas = [d for d in (_data(c.get("data_assinatura")) for c in contratos) if d]
    return {
        "disponivel": True,
        "total_contratos": len(contratos),
        "valor_acumulado": round(valor_total, 2),
        "orgaos_distintos": len(por_orgao),
        "principais_orgaos": sorted(por_orgao.values(), key=lambda o: (-o["valor"], -o["contratos"]))[:10],
        "recentes": recentes,
        "primeiro_contrato": min(datas) if datas else None,
        "ultimo_contrato": max(datas) if datas else None,
        "limitado": lidos < total_informado,  # há mais resultados além das páginas lidas
        "fonte": "PNCP (busca pública de contratos)",
    }


def buscar_fornecedores_por_nome(termo: str, limite: int = 10) -> List[Dict[str, str]]:
    """Empresas (CNPJ + nome) que aparecem como fornecedoras em contratos do PNCP com esse nome."""
    termo = (termo or "").strip()
    if len(termo) < 3:
        return []
    resposta = requisitar_json(PNCP_BUSCA_URL, params={
        "q": termo, "tipos_documento": "contrato", "pagina": 1, "tam_pagina": 100,
    }) or {}
    vistos: Dict[str, Dict[str, str]] = {}
    palavras = [p for p in re.split(r"\s+", termo.upper()) if p]
    for item in resposta.get("items") or []:
        cnpj = somente_digitos(item.get("fornecedor_ni"))
        nome = (item.get("fornecedor_nome") or "").strip()
        if len(cnpj) != 14 or cnpj in vistos or not all(p in nome.upper() for p in palavras):
            continue
        vistos[cnpj] = {"cnpj": cnpj, "cnpj_formatado": formatar_cnpj(cnpj), "nome": nome}
        if len(vistos) >= limite:
            break
    return list(vistos.values())


# ------------------------------------------------------------------------------
# Sanções (CEIS / CNEP — Portal da Transparência)
# ------------------------------------------------------------------------------
def _chave_transparencia() -> str:
    return (os.getenv("PORTAL_TRANSPARENCIA_API_KEY") or "").strip()


def consultar_sancoes(cnpj: str) -> Dict[str, Any]:
    """
    Sanções vigentes ou passadas no CEIS e no CNEP. Sem PORTAL_TRANSPARENCIA_API_KEY (cadastro
    gratuito no Portal da Transparência) a consulta não é feita e o resultado diz isso.
    """
    cnpj = normalizar_cnpj(cnpj)
    link = TRANSPARENCIA_CONSULTA_URL.format(cnpj=cnpj)
    chave = _chave_transparencia()
    if not chave:
        return {"verificado": False, "motivo": "Consulta automática ao CEIS/CNEP não configurada "
                "(PORTAL_TRANSPARENCIA_API_KEY).", "registros": [], "link": link}
    registros: List[Dict[str, Any]] = []
    try:
        for cadastro in ("ceis", "cnep"):
            itens = requisitar_json(TRANSPARENCIA_URL.format(cadastro=cadastro),
                                    params={"codigoSancionado": cnpj, "pagina": 1},
                                    headers={"chave-api-dados": chave}) or []
            for item in itens:
                sancionado = item.get("sancionado") or {}
                pessoa = item.get("pessoa") or {}
                documento = somente_digitos(sancionado.get("codigoFormatado") or pessoa.get("cnpjFormatado"))
                if documento != cnpj:  # nunca atribuir ao CNPJ uma sanção de outra pessoa
                    continue
                registros.append({
                    "cadastro": cadastro.upper(),
                    "tipo": (item.get("tipoSancao") or {}).get("descricaoResumida") or "",
                    "orgao": (item.get("orgaoSancionador") or {}).get("nome") or "",
                    "inicio": item.get("dataInicioSancao") or "",
                    "fim": item.get("dataFimSancao") or "",
                })
    except FonteIndisponivelError as exc:
        return {"verificado": False, "motivo": f"Portal da Transparência indisponível ({exc}).",
                "registros": [], "link": link}
    return {"verificado": True, "registros": registros, "link": link,
            "consultado_em": hoje_sao_paulo().isoformat(), "fonte": "Portal da Transparência (CEIS/CNEP)"}


# ------------------------------------------------------------------------------
# Matriz de risco
# ------------------------------------------------------------------------------
NIVEIS = {
    "verde": "Apto para Licitar",
    "amarelo": "Atenção Cadastral",
    "vermelho": "Inapto para Licitar",
}


def avaliar_risco(receita: Dict[str, Any], sancoes: Dict[str, Any], hoje: Optional[date] = None) -> Dict[str, Any]:
    """Selo de aptidão a partir das verificações feitas (o que não foi verificado nunca conta como ok)."""
    verificacoes: List[Dict[str, str]] = []
    nivel = "verde"

    def _rebaixar(para: str) -> None:
        nonlocal nivel
        ordem = ["verde", "amarelo", "vermelho"]
        if ordem.index(para) > ordem.index(nivel):
            nivel = para

    situacao = receita.get("situacao") or "NÃO INFORMADA"
    if situacao == "ATIVA":
        verificacoes.append({"item": "Situação cadastral na Receita", "resultado": "ok", "detalhe": "ATIVA"})
    else:
        _rebaixar("vermelho")
        motivo = f" ({receita['motivo_situacao']})" if receita.get("motivo_situacao") else ""
        verificacoes.append({"item": "Situação cadastral na Receita", "resultado": "falha",
                             "detalhe": f"{situacao}{motivo}"})

    if sancoes.get("verificado"):
        if sancoes.get("registros"):
            _rebaixar("vermelho")
            verificacoes.append({"item": "Sanções (CEIS/CNEP)", "resultado": "falha",
                                 "detalhe": f"{len(sancoes['registros'])} registro(s) de sanção"})
        else:
            verificacoes.append({"item": "Sanções (CEIS/CNEP)", "resultado": "ok", "detalhe": "Nada consta"})
    else:
        _rebaixar("amarelo")
        verificacoes.append({"item": "Sanções (CEIS/CNEP)", "resultado": "nao_verificado",
                             "detalhe": sancoes.get("motivo") or "Não verificado"})

    fundacao = tempo_de_fundacao(receita.get("data_abertura"), hoje)
    if fundacao is not None and fundacao["total_meses"] < 12:
        _rebaixar("amarelo")
        verificacoes.append({"item": "Tempo de fundação", "resultado": "alerta",
                             "detalhe": "Menos de 1 ano de atividade (atenção à qualificação econômico-financeira)"})
    elif fundacao is not None:
        verificacoes.append({"item": "Tempo de fundação", "resultado": "ok",
                             "detalhe": f"{fundacao['anos']} ano(s) e {fundacao['meses']} mês(es)"})

    return {"nivel": nivel, "rotulo": NIVEIS[nivel], "verificacoes": verificacoes}


# ------------------------------------------------------------------------------
# Dossiê completo (com cache de 24 h)
# ------------------------------------------------------------------------------
def montar_dossie(cnpj: str) -> Dict[str, Any]:
    cnpj = normalizar_cnpj(cnpj)
    receita = consultar_receita(cnpj)  # sem cadastro não há dossiê (erros sobem)
    try:
        contratos = consultar_contratos_pncp(cnpj)
    except FonteIndisponivelError as exc:
        contratos = {"disponivel": False, "motivo": str(exc)}
    sancoes = consultar_sancoes(cnpj)
    return {
        "cnpj": cnpj,
        "cnpj_formatado": formatar_cnpj(cnpj),
        "receita": receita,
        "fundacao": tempo_de_fundacao(receita.get("data_abertura")),
        "contratos": contratos,
        "sancoes": sancoes,
        "risco": avaliar_risco(receita, sancoes),
        "gerado_em": datetime.utcnow().isoformat(),
    }


def obter_dossie(db: Session, cnpj: str, atualizar: bool = False) -> Dict[str, Any]:
    """Dossiê do cache (até DOSSIE_CACHE_HORAS) ou recém-montado e gravado no cache."""
    cnpj = normalizar_cnpj(cnpj)
    registro = db.get(DossieFornecedor, cnpj)
    limite = datetime.utcnow() - timedelta(hours=DOSSIE_CACHE_HORAS)
    if registro and not atualizar and registro.atualizado_em >= limite:
        return {**registro.dados, "cache": True}
    dossie = montar_dossie(cnpj)
    if not dossie["contratos"].get("disponivel"):
        return {**dossie, "cache": False}  # PNCP fora do ar: não fixa um dossiê incompleto por 24 h
    if registro is None:
        registro = DossieFornecedor(cnpj=cnpj, dados=dossie)
        db.add(registro)
    else:
        registro.dados = dossie
    registro.atualizado_em = datetime.utcnow()
    db.commit()
    return {**dossie, "cache": False}
