import os
import re
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import google.genai as genai
except Exception:
    genai = None

# Sanitização de ambiente para evitar erros de proxy em ambientes restritos
if "NO_PROXY" in os.environ and "[" in os.environ["NO_PROXY"]:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
if "no_proxy" in os.environ and "[" in os.environ["no_proxy"]:
    os.environ["no_proxy"] = "localhost,127.0.0.1"


def get_gemini_api_key() -> Optional[str]:
    """Obtém a chave da API Gemini de variáveis de ambiente ou configurações."""
    for key_name in ["GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY"]:
        val = os.getenv(key_name)
        if val and val.strip():
            return val.strip()
    try:
        from server.config import settings
        for attr in ["GEMINI_API_KEY", "AI_API_KEY"]:
            val = getattr(settings, attr, None)
            if val and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    return None


def get_genai_client():
    """Instancia o cliente oficial google-genai se a API Key estiver configurada."""
    api_key = get_gemini_api_key()
    if not api_key:
        return None
    try:
        global genai
        if genai is None:
            import google.genai as genai
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.warning(f"Falha ao instanciar google.genai.Client: {e}")
        return None


def _clean_json_response(raw_text: str) -> Dict[str, Any]:
    """Limpa e decodifica texto formatado em JSON ou blocos markdown de código."""
    text = (raw_text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    return json.loads(text)


async def analisar_edital_documento(file_bytes: bytes, filename: str = "edital.pdf") -> Dict[str, Any]:
    """
    Analisa semanticamente o documento de edital/termo de referência (PDF ou texto)
    utilizando Google Gemini 2.5/2.0 Flash.
    Extrai:
    - itens (descrição, quantitativo, valores unitários e totais)
    - exigências de habilitação jurídica, fiscal e técnica
    - estimativa orçamentária global
    - pontos críticos de risco e sanções
    - score de confiança
    """
    if not file_bytes:
        return _fallback_analisar_edital_documento("", filename)

    # 1. Extração de texto de PDF com pipeline seguro
    extracted_text = ""
    is_pdf = filename.lower().endswith(".pdf") or file_bytes.startswith(b"%PDF-")

    if is_pdf:
        try:
            from server.pdf_processor import process_pdf_bytes
            res = process_pdf_bytes(file_bytes, max_pages=40)
            if res.success and res.full_text:
                extracted_text = res.full_text
        except Exception as err:
            logger.warning(f"Falha ao extrair texto do PDF via pdf_processor: {err}")

    if not extracted_text:
        try:
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            extracted_text = str(file_bytes[:4000])

    # Trunca se for gigantesco para respeitar limites práticos de prompt
    text_sample = extracted_text[:45000].strip()

    client = get_genai_client()
    if client:
        candidate_models = ["gemini-3.8-flash", "gemini-3.0-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
        prompt = f"""Você é um auditor sênior especialista em licitações públicas brasileiras (Lei 14.133/2021).
Analise o conteúdo do edital a seguir e extraia os elementos estritamente estruturados em JSON:
{text_sample}

Você DEVE responder ESTRITAMENTE em formato JSON com o seguinte schema exato:
{{
  "itens": [
    {{
      "item_numero": 1,
      "descricao": "Descrição clara do item",
      "quantidade": 1.0,
      "unidade": "UN",
      "valor_unitario_estimado": 0.0,
      "valor_total_estimado": 0.0
    }}
  ],
  "habilitacao_exigencias": [
    "Exigência 1 (ex: Atestado de capacidade técnica)",
    "Exigência 2 (ex: CNDT / Balanço Patrimonial)"
  ],
  "estimativa_orcamentaria": 0.0,
  "pontos_criticos_risco": [
    "Ponto de atenção ou risco crítico no prazo ou sanções"
  ],
  "confianca_score": 0.95
}}
"""
        for model in candidate_models:
            try:
                raw_output = None
                if hasattr(client, "aio") and hasattr(client.aio, "models"):
                    response = await client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config={"response_mime_type": "application/json"}
                    )
                    raw_output = getattr(response, "text", str(response))
                elif hasattr(client, "models"):
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=prompt,
                        config={"response_mime_type": "application/json"}
                    )
                    raw_output = getattr(response, "text", str(response))

                if raw_output:
                    parsed = _clean_json_response(raw_output)
                    if isinstance(parsed, dict) and "itens" in parsed:
                        parsed["confianca_score"] = float(parsed.get("confianca_score", 0.94))
                        parsed["modelo_utilizado"] = model
                        return parsed
            except Exception as e:
                logger.warning(f"Tentativa com Gemini ({model}) falhou: {e}. Tentando próximo modelo...")

    # Fallback determinístico inteligente
    return _fallback_analisar_edital_documento(text_sample, filename)


def _fallback_analisar_edital_documento(text: str, filename: str) -> Dict[str, Any]:
    """Gera extração heurística estruturada quando a IA externa está indisponível."""
    itens = []
    # Busca heurística por itens no texto (ex: Item 1, Item 01, Lote 1)
    item_matches = re.findall(r"(?:Item|Lote)\s*[:#º\s]*(\d+)[\s:-]+([^\n\r]+)", text, re.IGNORECASE)
    for num, desc in item_matches[:10]:
        itens.append({
            "item_numero": int(num),
            "descricao": desc.strip()[:200],
            "quantidade": 1.0,
            "unidade": "UN",
            "valor_unitario_estimado": 0.0,
            "valor_total_estimado": 0.0
        })

    if not itens:
        itens.append({
            "item_numero": 1,
            "descricao": f"Fornecimento / Prestação referente ao documento {filename}",
            "quantidade": 1.0,
            "unidade": "GLOBAL",
            "valor_unitario_estimado": 0.0,
            "valor_total_estimado": 0.0
        })

    # Busca estimativa orçamentária
    valor_match = re.search(r"R\$\s*([\d\.,]+)", text)
    est_val = 0.0
    if valor_match:
        try:
            val_clean = valor_match.group(1).replace(".", "").replace(",", ".")
            est_val = float(val_clean)
        except Exception:
            est_val = 0.0

    return {
        "itens": itens,
        "habilitacao_exigencias": [
            "Atestado de capacidade técnica com características compatíveis ao objeto licitado",
            "Certidão Negativa de Débitos Relativos aos Tributos Federais e à Dívida Ativa da União",
            "Regularidade perante o FGTS (CRF) e CNDT (Débitos Trabalhistas)",
            "Qualificação econômico-financeira (Certidão negativa de falência ou recuperação judicial)"
        ],
        "estimativa_orcamentaria": est_val,
        "pontos_criticos_risco": [
            "Prazo estrito para envio de esclarecimentos e impugnações (até 3 dias úteis antes da abertura)",
            "Necessidade de conferência minuciosa de prazos de entrega e garantias contratuais",
            "Sanções da Lei 14.133/2021 aplicáveis em caso de atraso na formalização da proposta"
        ],
        "confianca_score": 0.85,
        "modelo_utilizado": "Heurística de Fallback (Lei 14.133/2021)"
    }


async def resumo_executivo_oportunidade(dados_licitacao: Dict[str, Any]) -> Dict[str, Any]:
    """
    Produz resumo executivo de negócio de alto nível para a oportunidade com:
    - resumo: visão executiva objetiva do certame
    - riscos_juridicos: análise de cláusulas de barreira e exigências legais
    - competitividade_estimada: previsão de nível de concorrência ("Alta", "Média", "Baixa")
    - pontos_atencao: fatores críticos para vitória
    - recomendacao_participacao: parecer de viabilidade
    - confianca: score de confiança
    """
    objeto = dados_licitacao.get("object_description") or dados_licitacao.get("title") or "Objeto não informado"
    orgao = dados_licitacao.get("contracting_entity") or "Órgão não especificado"
    uf = dados_licitacao.get("state") or "BR"
    modalidade = dados_licitacao.get("modality") or "Pregão Eletrônico"
    val = dados_licitacao.get("estimated_total_value") or 0.0

    client = get_genai_client()
    if client:
        candidate_models = ["gemini-3.8-flash", "gemini-3.0-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
        prompt = f"""Você é o analista-chefe de inteligência em licitações da plataforma Licitações Brasil.
Gere um Resumo Executivo Direto ao Ponto para os diretores da empresa decidirem a participação neste certame:

DADOS DO CERTAME:
- Órgão Comprador: {orgao} ({uf})
- Modalidade: {modalidade}
- Valor Estimado: R$ {val:,.2f}
- Objeto / Termo: {objeto}

Responda ESTRITAMENTE em formato JSON com este schema:
{{
  "resumo": "Resumo executivo claro e objetivo do que está sendo contratado e objetivo público.",
  "riscos_juridicos": [
    "Risco jurídico 1 (ex: exigência restritiva potencial ou impugnação recomendada)",
    "Risco jurídico 2 (ex: penalidades severas)"
  ],
  "competitividade_estimada": "Alta/Média/Baixa com justificativa breve do mercado",
  "pontos_atencao": [
    "Ponto de atenção 1 (ex: prazo de entrega)",
    "Ponto de atenção 2 (ex: margem de desconto esperada)"
  ],
  "recomendacao_participacao": "Favorável / Com Ressalvas / Desfavorável",
  "confianca": 0.94
}}
"""
        for model in candidate_models:
            try:
                raw_output = None
                if hasattr(client, "aio") and hasattr(client.aio, "models"):
                    response = await client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config={"response_mime_type": "application/json"}
                    )
                    raw_output = getattr(response, "text", str(response))
                elif hasattr(client, "models"):
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=prompt,
                        config={"response_mime_type": "application/json"}
                    )
                    raw_output = getattr(response, "text", str(response))

                if raw_output:
                    parsed = _clean_json_response(raw_output)
                    if isinstance(parsed, dict) and "resumo" in parsed:
                        parsed["confianca"] = float(parsed.get("confianca", 0.93))
                        parsed["modelo_utilizado"] = model
                        return parsed
            except Exception as e:
                logger.warning(f"Gemini ({model}) falhou em resumo_executivo: {e}")

    # Fallback estruturado
    return _fallback_resumo_executivo_oportunidade(dados_licitacao)


def _fallback_resumo_executivo_oportunidade(dados: Dict[str, Any]) -> Dict[str, Any]:
    """Retorna análise executiva consistente baseada em regras da Lei 14.133/2021."""
    objeto = dados.get("object_description") or dados.get("title") or "Contratação pública"
    orgao = dados.get("contracting_entity") or "Órgão Comprador"
    uf = dados.get("state") or "Nacional"
    val = float(dados.get("estimated_total_value") or 0.0)

    val_str = f"R$ {val:,.2f}" if val > 0 else "Valor sigiloso ou sob cotação"

    comp = "Média"
    if val > 1000000:
        comp = "Alta - porte atrai grandes fornecedores e distribuidoras nacionais"
    elif val > 0 and val < 100000:
        comp = "Média/Baixa - atratividade voltada a ME/EPP e fornecedores regionais"

    return {
        "resumo": f"Oportunidade promovida por {orgao} ({uf}) com montante de {val_str}. O objeto visa: {objeto[:280]}.",
        "riscos_juridicos": [
            "Conferir conformidade dos prazos de impugnação previstos no art. 164 da Lei 14.133/2021.",
            "Validar exigência de índices contábeis (Liquidez Geral e Corrente) para certificar que não restringem a competitividade."
        ],
        "competitividade_estimada": comp,
        "pontos_atencao": [
            "Verificar especificação exata de marcas ou modelos similares aceitos pelo Termo de Referência.",
            "Planejar margem de stop-loss e decremento mínimo para disputa no cockpit de lances.",
            "Acompanhar eventuais pedidos de esclarecimento e avisos de retificação no PNCP."
        ],
        "recomendacao_participacao": "Favorável" if val > 0 else "Com Ressalvas",
        "confianca": 0.88,
        "modelo_utilizado": "Heurística de Fallback (Lei 14.133/2021)"
    }


class GeminiAnalyzer:
    """Wrapper utilitário para acesso ao serviço de IA Gemini."""
    analisar_edital_documento = staticmethod(analisar_edital_documento)
    resumo_executivo_oportunidade = staticmethod(resumo_executivo_oportunidade)


gemini_analyzer = GeminiAnalyzer()
