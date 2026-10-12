"""
Inteligência documental do Cofre (Licitações Brasil): classifica e extrai dados de qualquer documento
de habilitação (Lei 14.133/2021, arts. 62 a 70) com o Gemini Flash em saída estruturada (JSON Schema).

- Taxonomia: fiscal_trabalhista, juridica, financeira, tecnica, sancoes_declaracoes (ou "outro"),
  com tipos canônicos (cnd_federal, crf_fgts, cndt, balanco_patrimonial, act, cat...).
- Nada é aceito às cegas: categoria e tipo são conferidos contra a taxonomia, datas são validadas e
  ``dias_para_vencer`` / ``situacao`` são recalculados aqui, a partir da data de hoje (America/Sao_Paulo).
- Sem GEMINI_API_KEY ou com o Gemini fora do ar, cai num classificador por regras sobre o texto do
  PDF (palavras-chave, CNPJ e datas explícitas), marcado como ``metodo`` = "regras".
"""
import asyncio
import io
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from server.config import settings
from server.models import DOCUMENTO_DIAS_ALERTA, hoje_sao_paulo

logger = logging.getLogger(__name__)

CATEGORIAS = ("fiscal_trabalhista", "juridica", "financeira", "tecnica", "sancoes_declaracoes", "outro")
# Categoria da IA -> categoria do Cofre (DOCUMENTO_CATEGORIAS)
CATEGORIA_COFRE = {
    "fiscal_trabalhista": "fiscal", "juridica": "juridica", "financeira": "economica",
    "tecnica": "tecnica", "sancoes_declaracoes": "declaracoes", "outro": "outros",
}
SITUACOES = ("valido", "vencido", "atencao", "indeterminado")
TIMEOUT_GEMINI_SEGUNDOS = 90
TENTATIVAS_POR_MODELO = 2  # erro transitório (503/429/timeout) tenta de novo o mesmo modelo
ESPERA_RETENTATIVA_SEGUNDOS = 2.0

# tipo canônico -> (categoria, título formal, slot único?, palavras-chave para o classificador por regras)
# Slot único: o documento novo substitui o anterior do mesmo tipo (ex.: CND renovada). Os demais
# (atestados, CATs, licenças, declarações) se acumulam no Cofre.
TIPOS: Dict[str, Tuple[str, str, bool, Tuple[str, ...]]] = {
    "cnd_federal": ("fiscal_trabalhista", "Certidão Negativa de Débitos Federais e Dívida Ativa da União (Receita/PGFN)", True,
                    ("tributos federais e a divida ativa da uniao", "procuradoria-geral da fazenda nacional", "certidao negativa de debitos relativos aos tributos federais")),
    "crf_fgts": ("fiscal_trabalhista", "Certificado de Regularidade do FGTS (CRF)", True,
                 ("certificado de regularidade do fgts", "fundo de garantia do tempo de servico", "crf")),
    "cndt": ("fiscal_trabalhista", "Certidão Negativa de Débitos Trabalhistas (CNDT)", True,
             ("debitos trabalhistas", "banco nacional de devedores trabalhistas", "cndt")),
    "cnd_estadual": ("fiscal_trabalhista", "Certidão Negativa de Débitos Estaduais (ICMS)", True,
                     ("secretaria de estado da fazenda", "debitos estaduais", "icms")),
    "cnd_municipal": ("fiscal_trabalhista", "Certidão Negativa de Débitos Municipais (ISS)", True,
                      ("tributos municipais", "debitos municipais", "secretaria municipal de financas", "iss")),
    "divida_ativa": ("fiscal_trabalhista", "Certidão de Dívida Ativa", True, ("certidao de divida ativa",)),
    "cartao_cnpj": ("juridica", "Comprovante de Inscrição e Situação Cadastral (Cartão CNPJ)", True,
                    ("comprovante de inscricao e de situacao cadastral", "cadastro nacional da pessoa juridica")),
    "contrato_social": ("juridica", "Contrato Social / Consolidação / Estatuto", True,
                        ("contrato social", "alteracao contratual", "consolidacao", "estatuto social")),
    "certidao_junta": ("juridica", "Certidão Simplificada da Junta Comercial", True,
                       ("certidao simplificada", "junta comercial")),
    "documento_socio": ("juridica", "Documento Pessoal de Sócio (RG/CNH)", False,
                        ("carteira nacional de habilitacao", "registro geral", "carteira de identidade")),
    "inscricao_estadual": ("juridica", "Comprovante de Inscrição Estadual", True, ("inscricao estadual",)),
    "inscricao_municipal": ("juridica", "Comprovante de Inscrição Municipal", True, ("inscricao municipal", "cadastro mobiliario")),
    "falencia": ("financeira", "Certidão Negativa de Falência e Recuperação Judicial", True,
                 ("falencia", "recuperacao judicial", "concordata")),
    "balanco_patrimonial": ("financeira", "Balanço Patrimonial", False, ("balanco patrimonial", "ativo circulante", "passivo circulante")),
    "dre": ("financeira", "Demonstração do Resultado do Exercício (DRE)", False, ("demonstracao do resultado",)),
    "indices_contabeis": ("financeira", "Demonstrativo de Índices Contábeis (LG, SG, LC)", False,
                          ("liquidez geral", "solvencia geral", "liquidez corrente")),
    "crc_contador": ("financeira", "Certidão de Regularidade Profissional do Contador (CRC)", True,
                     ("conselho regional de contabilidade", "regularidade profissional")),
    "act": ("tecnica", "Atestado de Capacidade Técnica", False, ("atestado de capacidade tecnica", "atestamos", "atestado")),
    "cat": ("tecnica", "Certidão de Acervo Técnico (CAT/ART/RRT)", False,
            ("certidao de acervo tecnico", "anotacao de responsabilidade tecnica", "registro de responsabilidade tecnica")),
    "registro_conselho": ("tecnica", "Registro em Conselho de Classe (CREA, CRA, CAU...)", True,
                          ("certidao de registro e quitacao", "crea", "conselho regional de administracao", "cau")),
    "alvara": ("tecnica", "Alvará de Funcionamento", True, ("alvara de funcionamento", "alvara de licenca")),
    "licenca_sanitaria": ("tecnica", "Licença Sanitária (VISA/AFE)", False,
                          ("vigilancia sanitaria", "licenca sanitaria", "autorizacao de funcionamento", "anvisa")),
    "licenca_ambiental": ("tecnica", "Licença Ambiental", False, ("licenca ambiental", "licenca de operacao")),
    "sancoes": ("sancoes_declaracoes", "Certidão de Inidoneidade / Sanções (TCU, CEIS, CNEP, CNJ)", True,
                ("cadastro nacional de empresas inidoneas", "ceis", "cnep", "licitantes inidoneos", "improbidade administrativa")),
    "declaracao": ("sancoes_declaracoes", "Declaração Formal", False,
                   ("declara, sob as penas da lei", "declaramos", "declara para os devidos fins", "menor de dezoito")),
    "outro": ("outro", "Documento", False, ()),
}
TIPOS_SLOT_UNICO = {tipo for tipo, (_, _, unico, _) in TIPOS.items() if unico}

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "categoria": {"type": "string", "enum": list(CATEGORIAS)},
        "tipo_especifico": {"type": "string", "enum": list(TIPOS)},
        "titulo_documento": {"type": "string"},
        "orgao_emissor": {"type": "string"},
        "cnpj_cpf_identificado": {"type": ["string", "null"]},
        "razao_social_identificada": {"type": ["string", "null"]},
        "data_emissao": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "data_validade": {"type": ["string", "null"], "description": "YYYY-MM-DD; null se a validade for indeterminada"},
        "dias_para_vencer": {"type": ["integer", "null"]},
        "codigo_autenticidade": {"type": ["string", "null"]},
        "situacao": {"type": "string", "enum": list(SITUACOES)},
        "dados_especificos": {
            "type": "object",
            "properties": {
                "indices_contabeis": {
                    "type": "object",
                    "properties": {"lg": {"type": ["number", "null"]}, "sg": {"type": ["number", "null"]},
                                   "lc": {"type": ["number", "null"]}},
                    "required": ["lg", "sg", "lc"],
                },
                "objeto_atestado": {"type": ["string", "null"]},
                "exercicio_ano": {"type": ["integer", "null"]},
            },
            "required": ["indices_contabeis", "objeto_atestado", "exercicio_ano"],
        },
        "resumo_ia": {"type": "string"},
    },
    "required": ["categoria", "tipo_especifico", "titulo_documento", "orgao_emissor", "cnpj_cpf_identificado",
                 "razao_social_identificada", "data_emissao", "data_validade", "dias_para_vencer",
                 "codigo_autenticidade", "situacao", "dados_especificos", "resumo_ia"],
}

PROMPT = """Você é um analista de habilitação em licitações públicas brasileiras (Lei nº 14.133/2021, arts. 62 a 70).
Analise o documento anexo e devolva SOMENTE o JSON do schema.

Categorias:
- fiscal_trabalhista: CND Federal (Receita/PGFN), CRF do FGTS, CNDT, CND Estadual (ICMS), CND Municipal (ISS), Dívida Ativa.
- juridica: Cartão CNPJ, Contrato Social/Consolidação/Estatuto, Certidão Simplificada da Junta, documentos de sócios (RG/CNH), inscrições estadual/municipal.
- financeira: Certidão de Falência e Recuperação Judicial, Balanço Patrimonial, DRE, índices contábeis (LG, SG, LC), certidão do contador (CRC).
- tecnica: Atestado de Capacidade Técnica (act), CAT/ART/RRT (cat), registro em conselho (CREA, CRA...), alvará, licenças sanitárias (VISA/AFE) e ambientais.
- sancoes_declaracoes: certidões de inidoneidade (TCU, CEIS, CNEP, CNJ) e declarações formais assinadas (art. 7º CF, ME/EPP LC 123, proposta independente, reserva de vagas PCD...).
- outro: qualquer outro documento.

Regras:
- Use apenas o que está escrito no documento. Campo não encontrado = null. Nunca invente datas, números ou códigos.
- data_validade: a data de validade/vencimento impressa. Se o documento não tiver validade (contrato social, atestado, balanço), use null.
- Datas no formato YYYY-MM-DD.
- cnpj_cpf_identificado: o CNPJ ou CPF do titular do documento (a empresa), só dígitos.
- codigo_autenticidade: código de controle/verificação/autenticação, se houver.
- indices_contabeis: LG, SG e LC somente se o documento os informar ou trouxer os totais para calculá-los; senão null.
- objeto_atestado: para atestados e CATs, o serviço/fornecimento atestado em uma frase.
- exercicio_ano: ano do exercício social de balanços e DREs.
- resumo_ia: 1 a 2 frases objetivas sobre o que é o documento e a situação dele.
"""


class ExtracaoIndisponivel(RuntimeError):
    """Gemini não configurado, fora do ar ou com resposta inválida."""


# ------------------------------------------------------------------------------
# Normalização (vale para Gemini e regras)
# ------------------------------------------------------------------------------
def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii").lower()


def _data(valor: Any) -> Optional[date]:
    if not valor:
        return None
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto[:10], formato).date()
        except ValueError:
            continue
    return None


def _numero(valor: Any) -> Optional[float]:
    try:
        numero = float(str(valor).replace(",", ".")) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return round(numero, 4) if numero is not None and 0 <= numero < 1000 else None


def _inteiro(valor: Any) -> Optional[int]:
    try:
        return int(valor) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None


def situacao_por_validade(validade: Optional[date], hoje: date) -> Tuple[str, Optional[int]]:
    if validade is None:
        return "indeterminado", None
    dias = (validade - hoje).days
    if dias < 0:
        return "vencido", dias
    if dias <= DOCUMENTO_DIAS_ALERTA:
        return "atencao", dias
    return "valido", dias


def normalizar(bruto: Dict[str, Any], hoje: Optional[date] = None, metodo: str = "gemini") -> Dict[str, Any]:
    """Confere o retorno contra a taxonomia e recalcula prazos; nunca confia em dias/situação da IA."""
    hoje = hoje or hoje_sao_paulo()
    bruto = bruto if isinstance(bruto, dict) else {}
    tipo = str(bruto.get("tipo_especifico") or "").strip().lower()
    if tipo not in TIPOS:
        tipo = "outro"
    categoria = str(bruto.get("categoria") or "").strip().lower()
    if categoria not in CATEGORIAS or (tipo != "outro" and categoria != TIPOS[tipo][0]):
        categoria = TIPOS[tipo][0]  # o tipo reconhecido define a categoria legal
    alertas: List[str] = []
    emissao = _data(bruto.get("data_emissao"))
    validade = _data(bruto.get("data_validade"))
    if emissao and emissao > hoje:
        alertas.append("Data de emissão no futuro: confira o documento.")
        emissao = None
    if emissao and validade and validade < emissao:
        alertas.append("Validade anterior à emissão: validade desconsiderada.")
        validade = None
    situacao, dias = situacao_por_validade(validade, hoje)
    documento = re.sub(r"\D", "", str(bruto.get("cnpj_cpf_identificado") or ""))
    especificos = bruto.get("dados_especificos") if isinstance(bruto.get("dados_especificos"), dict) else {}
    indices = especificos.get("indices_contabeis") if isinstance(especificos.get("indices_contabeis"), dict) else {}
    titulo = str(bruto.get("titulo_documento") or "").strip()[:255] or TIPOS[tipo][1]
    return {
        "categoria": categoria,
        "tipo_especifico": tipo,
        "titulo_documento": titulo,
        "orgao_emissor": (str(bruto.get("orgao_emissor") or "").strip() or None),
        "cnpj_cpf_identificado": documento if len(documento) in (11, 14) else None,
        "razao_social_identificada": (str(bruto.get("razao_social_identificada") or "").strip() or None),
        "data_emissao": emissao.isoformat() if emissao else None,
        "data_validade": validade.isoformat() if validade else None,
        "dias_para_vencer": dias,
        "codigo_autenticidade": (str(bruto.get("codigo_autenticidade") or "").strip()[:120] or None),
        "situacao": situacao,
        "dados_especificos": {
            "indices_contabeis": {k: _numero(indices.get(k)) for k in ("lg", "sg", "lc")},
            "objeto_atestado": (str(especificos.get("objeto_atestado") or "").strip()[:1000] or None),
            "exercicio_ano": _inteiro(especificos.get("exercicio_ano")),
        },
        "resumo_ia": str(bruto.get("resumo_ia") or "").strip()[:1000],
        "metodo": metodo,
        "alertas": alertas,
        "analisado_em": datetime.utcnow().isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------------------
# Gemini Flash (saída estruturada)
# ------------------------------------------------------------------------------
def modelos_candidatos() -> List[str]:
    # Gemini Flash primeiro (a API recusa modelos antigos para contas novas); AI_MODEL e anteriores como reserva
    candidatos = [os.getenv("DOCUMENT_AI_MODEL"), "gemini-3.8-flash", settings.AI_MODEL, "gemini-3.0-flash"]
    vistos: List[str] = []
    for m in candidatos:
        if m and m not in vistos:
            vistos.append(m)
    return vistos


def _cliente_gemini():
    from server.services.gemini_analyzer import get_genai_client
    return get_genai_client()


async def extrair_com_gemini(conteudo: bytes, mime_type: str, nome_arquivo: str) -> Dict[str, Any]:
    cliente = _cliente_gemini()
    if cliente is None:
        raise ExtracaoIndisponivel("GEMINI_API_KEY não configurada.")
    from google.genai import types

    partes = [types.Part.from_bytes(data=conteudo, mime_type=mime_type),
              f"{PROMPT}\nNome do arquivo enviado: {nome_arquivo}"]
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=SCHEMA, temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    ultimo_erro: Optional[Exception] = None
    for modelo in modelos_candidatos():
        for tentativa in range(1, TENTATIVAS_POR_MODELO + 1):
            try:
                resposta = await asyncio.wait_for(
                    cliente.aio.models.generate_content(model=modelo, contents=partes, config=config),
                    timeout=TIMEOUT_GEMINI_SEGUNDOS)
                texto = (getattr(resposta, "text", None) or "").strip()
                if texto.startswith("```"):
                    texto = texto.strip("`").removeprefix("json").strip()
                return json.loads(texto)
            except (ValueError, TypeError) as exc:  # JSON inválido: não adianta trocar de modelo
                raise ExtracaoIndisponivel(f"Resposta do Gemini fora do schema: {exc}") from exc
            except Exception as exc:  # modelo inexistente, cota, sobrecarga, rede
                ultimo_erro = exc
                logger.info("Gemini (%s, tentativa %s) falhou para %s: %r", modelo, tentativa, nome_arquivo, exc)
                if not _erro_transitorio(exc) or tentativa == TENTATIVAS_POR_MODELO:
                    break  # próximo modelo
                await asyncio.sleep(ESPERA_RETENTATIVA_SEGUNDOS * tentativa)
    raise ExtracaoIndisponivel(f"Gemini indisponível ({ultimo_erro!r}).")


def _erro_transitorio(exc: Exception) -> bool:
    texto = repr(exc).upper()
    return isinstance(exc, asyncio.TimeoutError) or any(
        marca in texto for marca in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "DEADLINE"))


# ------------------------------------------------------------------------------
# Classificador por regras (sem IA)
# ------------------------------------------------------------------------------
_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_RE_DATA = r"(\d{2}/\d{2}/\d{4})"
_RE_VALIDADE = re.compile(r"(?:valid[ao]\s+ate|validade\s*:?|vencimento\s*:?|valida\s+por\s+\d+\s+dias.*?ate)\s*(?:o\s+dia\s+)?" + _RE_DATA)
_RE_EMISSAO = re.compile(r"(?:emitid[ao]\s+(?:em|no\s+dia)|data\s+(?:da\s+)?emissao\s*:?|expedid[ao]\s+em)\s*" + _RE_DATA)
_RE_CODIGO = re.compile(r"(?:codigo\s+de\s+controle(?:\s+da\s+certidao)?|codigo\s+de\s+(?:autenticidade|verificacao|autenticacao)|chave\s+de\s+validacao)\s*:?\s*([a-z0-9.\-]{6,60})")


def texto_do_pdf(conteudo: bytes, max_paginas: int = 8) -> str:
    try:
        from pypdf import PdfReader
        leitor = PdfReader(io.BytesIO(conteudo))
        return "\n".join((pagina.extract_text() or "") for pagina in leitor.pages[:max_paginas])
    except Exception:
        return ""


def extrair_por_regras(conteudo: bytes, mime_type: str, nome_arquivo: str, motivo: str = "") -> Dict[str, Any]:
    texto = texto_do_pdf(conteudo) if mime_type == "application/pdf" else ""
    base = _sem_acento(f"{nome_arquivo}\n{texto}")
    melhor, pontos = "outro", 0
    for tipo, (_, _, _, palavras) in TIPOS.items():
        acertos = sum(1 for p in palavras if p and p in base)
        if acertos > pontos:
            melhor, pontos = tipo, acertos
    cnpj = _RE_CNPJ.search(texto)
    validade = _RE_VALIDADE.search(base)
    emissao = _RE_EMISSAO.search(base)
    codigo = _RE_CODIGO.search(base)
    leitura = "texto do PDF" if texto.strip() else "nome do arquivo (sem texto legível: imagem ou PDF digitalizado)"
    resumo = f"Classificado por regras a partir do {leitura}{f' — {motivo}' if motivo else ''}. Confira os dados."
    return {
        "categoria": TIPOS[melhor][0], "tipo_especifico": melhor, "titulo_documento": TIPOS[melhor][1],
        "orgao_emissor": None, "cnpj_cpf_identificado": cnpj.group(0) if cnpj else None,
        "razao_social_identificada": None,
        "data_emissao": emissao.group(1) if emissao else None,
        "data_validade": validade.group(1) if validade else None,
        "codigo_autenticidade": codigo.group(1).upper() if codigo else None,
        "dados_especificos": {"indices_contabeis": {}, "objeto_atestado": None, "exercicio_ano": None},
        "resumo_ia": resumo,
    }


async def analisar_documento(conteudo: bytes, mime_type: str, nome_arquivo: str,
                             hoje: Optional[date] = None) -> Dict[str, Any]:
    """Extração estruturada e normalizada: Gemini Flash; sem ele, classificador por regras."""
    try:
        bruto = await extrair_com_gemini(conteudo, mime_type, nome_arquivo)
        return normalizar(bruto, hoje, "gemini")
    except ExtracaoIndisponivel as exc:
        motivo = "IA indisponível" if "GEMINI_API_KEY" in str(exc) else f"Gemini indisponível: {exc}"
        resultado = normalizar(extrair_por_regras(conteudo, mime_type, nome_arquivo, motivo), hoje, "regras")
        resultado["alertas"].insert(0, "Classificação sem IA: revise categoria, datas e órgão emissor.")
        return resultado


# ------------------------------------------------------------------------------
# Gravação no Cofre (isolada por conta)
# ------------------------------------------------------------------------------
def cnpj_de_referencia(db, user_id: str) -> Optional[str]:
    """CNPJ da empresa da conta: o usado na importação automática de certidões (mais recente)."""
    from server.models import DocumentoEmpresa
    linha = (db.query(DocumentoEmpresa.cnpj)
             .filter(DocumentoEmpresa.user_id == user_id, DocumentoEmpresa.origem == "CNPJ",
                     DocumentoEmpresa.cnpj.isnot(None))
             .order_by(DocumentoEmpresa.created_at.desc()).first())
    return linha[0] if linha else None


def _slot_existente(db, user_id: str, tipo: str, validade_nova: Optional[date], hoje: date):
    """Documento do mesmo tipo de slot único a ser atualizado: pendente, vencido ou versão anterior."""
    from server.models import DocumentoEmpresa
    if tipo not in TIPOS_SLOT_UNICO:
        return None
    candidatos = (db.query(DocumentoEmpresa)
                  .filter(DocumentoEmpresa.user_id == user_id, DocumentoEmpresa.tipo_documento == tipo)
                  .order_by(DocumentoEmpresa.pendente.desc(), DocumentoEmpresa.updated_at.desc()).all())
    for doc in candidatos:
        if doc.pendente or doc.status_validade(hoje) == "VENCIDO":
            return doc
        if validade_nova is None and doc.data_validade is None:
            return doc  # ex.: nova consolidação do contrato social
        if validade_nova and doc.data_validade and doc.data_validade <= validade_nova:
            return doc  # renovação da certidão
    return None


def gravar_no_cofre(db, user_id: str, conteudo: bytes, mime_type: str, nome_arquivo: str,
                    analise: Dict[str, Any], hoje: Optional[date] = None):
    """
    Grava o documento analisado no Cofre da conta: atualiza o slot do mesmo tipo (pendente, vencido
    ou versão anterior de um documento único) ou cria um novo. Devolve (documento, "criado"|"atualizado").
    """
    from server.models import DocumentoEmpresa
    from server.services import document_vault

    hoje = hoje or hoje_sao_paulo()
    analise = dict(analise)
    alertas = list(analise.get("alertas") or [])
    referencia = cnpj_de_referencia(db, user_id)
    identificado = analise.get("cnpj_cpf_identificado")
    if referencia and identificado and len(identificado) == 14 and identificado != referencia:
        alertas.append("CNPJ divergente do cadastro")
    analise["alertas"] = alertas
    analise["arquivo_original"] = (nome_arquivo or "")[:255]

    tipo = analise["tipo_especifico"]
    emissao = _data(analise.get("data_emissao"))
    validade = _data(analise.get("data_validade"))
    categoria = CATEGORIA_COFRE[analise["categoria"]]
    doc = _slot_existente(db, user_id, tipo, validade, hoje)
    acao = "atualizado" if doc else "criado"
    referencia_arquivo = document_vault.salvar_arquivo_no_banco(db, conteudo, analise["titulo_documento"], user_id, mime_type)
    if doc is None:
        doc = DocumentoEmpresa(user_id=user_id, origem="IA", created_at=datetime.utcnow())
        db.add(doc)
        doc.nome = analise["titulo_documento"]
        doc.tags_habilitacao = document_vault.normalizar_tags(TIPOS[tipo][1].split(" (")[0])
    else:
        anterior = doc.caminho_arquivo
        if anterior:
            document_vault.remover_arquivo_documento(anterior, db)
        if doc.origem != "CNPJ":  # slots da importação por CNPJ mantêm o nome oficial
            doc.nome = analise["titulo_documento"]
    doc.categoria = categoria
    doc.tipo_documento = tipo
    doc.caminho_arquivo = referencia_arquivo
    doc.data_emissao = emissao or hoje  # sem emissão identificada: data do registro (não exibida como emissão)
    doc.data_validade = validade
    doc.pendente = False
    doc.cnpj = identificado if identificado and len(identificado) == 14 else doc.cnpj
    doc.metadados_ia = analise
    doc.updated_at = datetime.utcnow()
    db.flush()
    return doc, acao
