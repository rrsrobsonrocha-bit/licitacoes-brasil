import os
import json
import logging
import asyncio
import inspect
from typing import Dict, Any, Optional, List
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.orm import Session

from server.config import settings
from server.services import gemini_chaves
from server.database import SessionLocal
from server.models import Tender, TenderItem, TenderDocument, AIAnalysis
from server.security import calculate_sha256

logger = logging.getLogger(__name__)

# Sanitização de ambiente para httpx (evitar erro de parse de IPv6 do NO_PROXY no sandbox)
if "NO_PROXY" in os.environ and "[" in os.environ["NO_PROXY"]:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
if "no_proxy" in os.environ and "[" in os.environ["no_proxy"]:
    os.environ["no_proxy"] = "localhost,127.0.0.1"

SYSTEM_SUMMARY_INSTRUCTION = """Você é um especialista sênior em licitações e contratações públicas brasileiras (Lei nº 14.133/2021) e analista executivo de negócios.
Sua missão é realizar a análise estritamente técnica, objetiva e estruturada de editais para gerar um Resumo Executivo Direto ao Ponto.

Você DEVE responder SEMPRE no formato JSON válido com as seguintes chaves:
{
  "objeto_resumido": "Descrição objetiva, clara e em linguagem acessível do que está sendo contratado.",
  "habilitacao_tecnica": [
    "Exigência crítica de capacidade técnica ou operacional 1 (ex: atestados de capacidade, CAT, CREA/CRM/CRF)",
    "Exigência crítica 2 (ex: qualificação de equipe, certificações ou vistorias técnicas)"
  ],
  "prazos_entrega_execucao": "Prazos de entrega/execução, início de vigência e cronograma previsto.",
  "condicoes_pagamento_sancoes": "Regras de liquidação/pagamento (ex: até 30 dias após ateste) e sanções/penalidades previstas em caso de descumprimento conforme Lei 14.133/2021.",
  "pontos_atencao": [
    "Ponto de atenção ou risco para o licitante 1",
    "Ponto de atenção ou risco 2"
  ]
}

REGRAS:
1. NUNCA invente dados. Caso um aspecto não conste explicitamente no extrato, mencione as disposições gerais da Lei 14.133/2021 ou indique que o licitante deve conferir no Termo de Referência.
2. Foque na viabilidade e na tomada de decisão de participação do licitante.
3. Responda APENAS o JSON válido.
"""

SYSTEM_CHAT_INSTRUCTION = """Você é um consultor sênior especialista em licitações públicas brasileiras e na Lei nº 14.133/2021 ("Converse com o Edital").
Sua missão é responder dúvidas pontuais de fornecedores e licitantes sobre o certame em análise.

DIRETRIZES:
1. Responda em tom estritamente objetivo, consultivo, prestativo e profissional.
2. Utilize as informações contextuais do certame fornecido (objeto, itens, prazos, órgão e valores).
3. Se a informação solicitada constar no edital/extrato, responda diretamente e ressalte os pontos práticos.
4. Se o extrato não contiver o detalhe específico (ex: fórmula exata de reajuste ou modelo de anexo), seja transparente, oriente o fornecedor com base nas regras gerais da Lei 14.133/2021 e recomende a consulta ao Termo de Referência ou o envio de pedido de esclarecimento oficial ao órgão em até 3 dias úteis antes da abertura (art. 164 da Lei 14.133/2021).
5. Sempre cite artigos pertinentes da Lei nº 14.133/2021 quando fundamentar direitos e deveres.
"""


def _get_api_key() -> str:
    """Primeira chave do Gemini configurada (GEMINI_API_KEYS ou GEMINI_API_KEY); vazia = fallback local."""
    chaves = gemini_chaves.chaves_configuradas()
    return chaves[0] if chaves else ""


def _cliente_gemini(api_key: str) -> "gemini_chaves.ClienteRotativo":
    """Cliente com rotação entre todas as chaves configuradas (troca de chave quando a cota esgota)."""
    return gemini_chaves.ClienteRotativo(gemini_chaves.chaves_configuradas() or [api_key])


def _normalize_model_name(name: str) -> str:
    """Mapeia aliases estáveis para os endpoints compatíveis do Google GenAI v1beta"""
    aliases = {
        "gemini-3.0-flash": "gemini-3.5-flash",
        "gemini-3.8-flash": "gemini-3.5-flash"
    }
    return aliases.get(name, name)


async def _execute_with_retry_and_timeout(
    coro_fn,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    timeout: float = 30.0
):
    """
    Executa chamada assíncrona de IA com timeout explícito de 30s
    e retry com backoff exponencial para erros transitórios (HTTP 429 e 503).
    """
    delay = initial_delay
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(coro_fn(), timeout=timeout)
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_transient = any(
                code in err_str
                for code in ["429", "503", "resource_exhausted", "unavailable", "rate limit", "temporarily unavailable"]
            )
            if is_transient and attempt < max_retries:
                logger.warning(
                    f"Erro transitório na chamada de IA ({type(e).__name__}: {e}). "
                    f"Tentativa {attempt + 1}/{max_retries} em {delay:.1f}s com backoff..."
                )
                await asyncio.sleep(delay)
                delay *= 2.0
            else:
                raise last_err


def _build_tender_context(tender: Tender, items: List[TenderItem], docs: List[TenderDocument]) -> str:
    """Monta o extrato textual estruturado do certame para servir de contexto para a IA"""
    items_text = []
    for it in items[:15]:
        val_str = f"R$ {it.estimated_total_value:,.2f}" if it.estimated_total_value else "Não informado"
        items_text.append(f"- Item {it.item_number}: {it.description} | Qtd: {it.quantity or 1} {it.unit or 'UN'} | Estimado: {val_str}")
    
    items_block = "\n".join(items_text) if items_text else "Sem itens detalhados cadastrados no extrato."
    
    docs_text = ""
    for d in docs:
        if d.extraction and d.extraction.extracted_text_reference:
            docs_text += f"\n--- Trecho de Anexo ({d.name}) ---\n{d.extraction.extracted_text_reference[:3000]}\n"
    
    val_total = f"R$ {tender.estimated_total_value:,.2f}" if tender.estimated_total_value else "Sigiloso ou a definir"
    data_abertura = tender.opening_date.strftime("%d/%m/%Y às %H:%M") if tender.opening_date else "Não informada"
    data_limite = tender.proposal_deadline.strftime("%d/%m/%Y às %H:%M") if tender.proposal_deadline else "Não informada"

    context = f"""DADOS DO CERTAME:
- Identificador / Certame: {tender.notice_number or tender.external_id}
- Título Oficial: {tender.title}
- Órgão Comprador: {tender.contracting_entity} ({tender.city or 'DF'} - {tender.state})
- Modalidade: {tender.modality}
- Regime / Sistema: {tender.source_system} (UASG: {tender.uasg_code or 'N/A'})
- Valor Estimado Total: {val_total}
- Data Limite de Propostas: {data_limite}
- Data de Abertura / Disputa: {data_abertura}

OBJETO DA CONTRATAÇÃO:
{tender.object_description}

ITENS REGISTRADOS:
{items_block}
{docs_text}
"""
    return context


def _generate_local_fallback_summary(tender: Tender, items: List[TenderItem]) -> Dict[str, Any]:
    """Gera um resumo executivo estruturado com base nas regras de conformidade da Lei 14.133/2021"""
    total_val = f"R$ {tender.estimated_total_value:,.2f}" if tender.estimated_total_value else "Conforme edital"
    
    is_servico = (tender.category or "").upper() == "SERVICOS" or "serviço" in tender.object_description.lower()
    is_dispensa = "dispensa" in (tender.modality or "").lower()

    if is_dispensa:
        hab_tecnica = [
            "Habilitação simplificada conforme art. 75 da Lei nº 14.133/2021",
            "Apresentação de declaração de capacidade operacional e regularidade cadastral (SICAF/Certidões)",
            "Comprovação de adequação aos requisitos do Termo de Referência/Aviso de Contratação Direta"
        ]
        prazos = "Entrega imediata ou prazo de até 30 dias contados da emissão da Nota de Empenho/Ordem de Fornecimento."
    elif is_servico:
        hab_tecnica = [
            "Atestado(s) de capacidade técnica emitido(s) por PJ de direito público ou privado comprovando execução de serviços similares",
            "Registro ou inscrição da empresa e responsável técnico no conselho de classe competente (ex: CRA, CREA, CFT)",
            "Declaração de disponibilidade de equipe técnica capacitada e instalações adequadas"
        ]
        prazos = "Execução contínua ou parcelada com vigência contratual inicial de 12 meses (prorrogável até 5 ou 10 anos, art. 106/107 da Lei 14.133/2021)."
    else:
        hab_tecnica = [
            "Atestado(s) de capacidade técnica compatível em características e quantitativos com os itens cotados",
            "Certidões de regularidade perante a Receita Federal, CNDT e FGTS",
            "Comprovação de que os produtos atendem às normas técnicas brasileiras (ABNT/Inmetro) e garantia de fábrica"
        ]
        prazos = "Entrega dos bens em até 30 (trinta) dias úteis após o recebimento da Nota de Empenho, com recebimento provisório e definitivo."

    pagamento = (
        "Pagamento em até 30 (trinta) dias úteis após ateste e liquidação da Nota Fiscal eletrônica. "
        "Sanções aplicáveis: advertência, multa compensatória/moratória (0,5% a 30%), "
        "impedimento de licitar e declaração de inidoneidade, nos termos dos arts. 155 e 156 da Lei nº 14.133/2021."
    )

    pontos_atencao = [
        f"Certame com valor estimado de {total_val}. Verifique margem de lance e custos operacionais.",
        "Prazo de impugnação e pedidos de esclarecimento até 3 dias úteis antes da abertura oficial.",
        "Verifique com atenção se há exigência de catálogo técnico, amostra ou vistoria técnica facultativa/obrigatória."
    ]

    return {
        "objeto_resumido": tender.object_description[:400] if tender.object_description else tender.title,
        "habilitacao_tecnica": hab_tecnica,
        "prazos_entrega_execucao": prazos,
        "condicoes_pagamento_sancoes": pagamento,
        "pontos_atencao": pontos_atencao
    }


def _generate_local_fallback_answer(tender: Tender, items: List[TenderItem], question: str) -> str:
    """Gera resposta consultiva inteligente contextualizada com a Lei 14.133/2021"""
    q_lower = question.lower().strip()
    val_str = f"R$ {tender.estimated_total_value:,.2f}" if tender.estimated_total_value else "sigiloso ou a ser apurado na disputa"
    prazo_abertura = tender.opening_date.strftime("%d/%m/%Y às %H:%M") if tender.opening_date else "a consultar no sistema"

    if "prazo" in q_lower or "quando" in q_lower or "abertura" in q_lower or "data" in q_lower:
        return (
            f"📅 **Cronograma do Certame**:\n\n"
            f"- **Abertura da sessão pública/lances**: {prazo_abertura}.\n"
            f"- **Limite de envio de propostas**: "
            f"{tender.proposal_deadline.strftime('%d/%m/%Y às %H:%M') if tender.proposal_deadline else 'Até o momento de abertura da sessão'}.\n\n"
            f"💡 **Recomendação Consultiva**: Em pregões e dispensas eletrônicas (Lei 14.133/2021), "
            f"cadastre suas propostas e documentos com antecedência mínima de algumas horas para prevenir instabilidades técnicas no portal oficial."
        )

    if "valor" in q_lower or "preço" in q_lower or "preco" in q_lower or "estimado" in q_lower or "orçamento" in q_lower:
        return (
            f"💰 **Valores do Certame**:\n\n"
            f"- O valor total estimado registrado é de **{val_str}**.\n"
            f"- Itens cadastrados: {len(items)} item(ns).\n\n"
            f"💡 **Recomendação Consultiva**: De acordo com a Lei nº 14.133/2021, propostas com valor superior "
            f"ao limite estimado podem ser desclassificadas, salvo se houver margem de negociação com o Agente de Contratação/Pregoeiro."
        )

    if "habilita" in q_lower or "atestado" in q_lower or "document" in q_lower or "exig" in q_lower:
        return (
            f"📑 **Exigências de Habilitação (Lei 14.133/2021)**:\n\n"
            f"Para este certame do(a) **{tender.contracting_entity}**, aplicam-se:\n"
            f"1. **Habilitação Jurídica e Fiscal**: Contrato Social, CNPJ ativo, CND Federal/Trabalhista (CNDT) e FGTS regularizados no SICAF.\n"
            f"2. **Qualificação Técnica**: Atestado de capacidade técnica compatível em características com o objeto ({tender.object_description[:100]}...).\n"
            f"3. **Qualificação Econômico-Financeira**: Balanço Patrimonial e certidão negativa de falência.\n\n"
            f"💡 **Importante**: Conforme art. 67 da Lei 14.133/2021, o órgão só pode exigir atestados com quantitativos de até 50% das parcelas de maior relevância."
        )

    if "pagamento" in q_lower or "pagar" in q_lower or "multa" in q_lower or "sanç" in q_lower or "sanc" in q_lower:
        return (
            f"💳 **Pagamento e Sanções**:\n\n"
            f"- **Prazo de Pagamento**: Conforme padrão da Lei 14.133/2021 (art. 141 e segs.), os pagamentos ocorrem "
            f"em até 30 dias após o ateste da liquidação da despesa pelo fiscal do contrato.\n"
            f"- **Sanções Administrativas**: Advertência, multa (moratória ou compensatória), impedimento de licitar e contratar "
            f"por até 3 anos, ou declaração de inidoneidade (art. 156 da Lei 14.133/2021).\n\n"
            f"💡 Recomenda-se prever capital de giro suficiente para cobrir o ciclo de faturamento da Administração."
        )

    if "esclarecimento" in q_lower or "impugna" in q_lower or "recurso" in q_lower:
        return (
            f"⚖️ **Impugnação e Esclarecimentos (Lei 14.133/2021, Art. 164)**:\n\n"
            f"- Qualquer pessoa física ou jurídica é parte legítima para impugnar o edital ou solicitar esclarecimentos.\n"
            f"- **Prazo limite**: até **3 (três) dias úteis** antes da data fixada para abertura do certame.\n"
            f"- A resposta do Agente de Contratação/Pregoeiro deve ser divulgada no sítio oficial no prazo de até 3 dias úteis.\n\n"
            f"💡 Nosso módulo **Jurídico** do Licitações Brasil possui minutas prontas de impugnação e pedidos de esclarecimento fundamentadas!"
        )

    return (
        f"🤖 **Análise Consultiva sobre o Certame**:\n\n"
        f"Em relação à sua dúvida (*\"{question}\"*), o certame promovido pelo(a) **{tender.contracting_entity}** "
        f"tem por objeto: *{tender.object_description[:250]}*...\n\n"
        f"📌 **Orientação Estratégica**: Para obter a resposta conclusiva específica sobre essa cláusula:\n"
        f"1. Verifique os Anexos e Termo de Referência disponibilizados no botão **'Arquivos do Edital'**.\n"
        f"2. Caso o edital seja omisso ou ambíguo, formule um **Pedido de Esclarecimento** com base no art. 164 da Lei 14.133/2021.\n\n"
        f"Fique à vontade para perguntar sobre prazos, documentos de habilitação, valores ou impugnações!"
    )


async def analyze_tender_summary(
    tender_id: str,
    db: Optional[Session] = None,
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Gera ou recupera em cache um Resumo Executivo Estruturado do certame com IA Gemini:
    - Objeto resumido em linguagem clara
    - Exigências de Habilitação Técnica mais críticas
    - Prazos de entrega/execução
    - Condições de pagamento e sanções
    """
    should_close_db = False
    if db is None:
        db = SessionLocal()
        should_close_db = True

    try:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            raise HTTPException(status_code=404, detail="Licitação não encontrada.")

        # Verificar se já existe cache
        if not force_refresh:
            cached = (
                db.query(AIAnalysis)
                .filter(
                    AIAnalysis.tender_id == tender_id,
                    AIAnalysis.analysis_type == "EXECUTIVE_SUMMARY"
                )
                .order_by(AIAnalysis.created_at.desc())
                .first()
            )
            if cached and cached.result_json:
                return {
                    "success": True,
                    "cached": True,
                    "tender_id": tender.id,
                    "provider": cached.model_provider,
                    "model": cached.model_name,
                    "created_at": cached.created_at.isoformat() if cached.created_at else None,
                    "summary": cached.result_json
                }

        # Carregar itens e documentos associados
        items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
        docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
        
        context_payload = _build_tender_context(tender, items, docs)
        user_prompt = f"Analise o certame a seguir e forneça o Resumo Executivo Estruturado em JSON:\n\n{context_payload}"

        api_key = _get_api_key()
        provider = "GEMINI"
        model_name = settings.AI_MODEL or "gemini-3.0-flash"
        summary_result: Optional[Dict[str, Any]] = None

        if api_key:
            try:
                client = _cliente_gemini(api_key)

                candidate_models = list(dict.fromkeys([model_name, "gemini-3.0-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]))
                for candidate in candidate_models:
                    target_model = _normalize_model_name(candidate)
                    try:
                        async def _do_summary_call():
                            if hasattr(client.aio, "chats"):
                                chat = client.aio.chats.create(
                                    model=target_model,
                                    config={
                                        "system_instruction": SYSTEM_SUMMARY_INSTRUCTION,
                                        "response_mime_type": "application/json"
                                    }
                                )
                                send_call = chat.send_message(message=user_prompt)
                                if inspect.isawaitable(send_call):
                                    return await send_call

                            if hasattr(client.aio, "models") and hasattr(client.aio.models, "generate_content"):
                                gen_call = client.aio.models.generate_content(
                                    model=target_model,
                                    contents=user_prompt,
                                    config={
                                        "system_instruction": SYSTEM_SUMMARY_INSTRUCTION,
                                        "response_mime_type": "application/json"
                                    }
                                )
                                if inspect.isawaitable(gen_call):
                                    return await gen_call
                                return gen_call
                            return None

                        response = await _execute_with_retry_and_timeout(
                            _do_summary_call,
                            max_retries=2,
                            initial_delay=1.0,
                            timeout=30.0
                        )

                        if response is not None:
                            raw_text = response.text.strip()
                            summary_result = json.loads(raw_text)
                            model_name = candidate
                            break
                    except Exception as model_err:
                        logger.warning(f"Tentativa com modelo {candidate} falhou ({type(model_err).__name__}): {model_err or 'timeout/erro interno'}")
                        continue
            except Exception as e:
                logger.warning(f"Erro ao chamar Google GenAI para resumo ({e}). Ativando fallback estruturado.")

        if not summary_result:
            # Fallback seguro estruturado
            provider = "LOCAL_COMPLIANCE_ENGINE"
            model_name = "rule-based-executive-v1"
            summary_result = _generate_local_fallback_summary(tender, items)

        # Salvar em cache no banco de dados
        record = AIAnalysis(
            tender_id=tender.id,
            model_provider=provider,
            model_name=model_name,
            analysis_type="EXECUTIVE_SUMMARY",
            result_json=summary_result,
            source_text_hash=calculate_sha256(context_payload.encode("utf-8"))
        )
        db.add(record)
        db.commit()

        return {
            "success": True,
            "cached": False,
            "tender_id": tender.id,
            "provider": provider,
            "model": model_name,
            "created_at": datetime.utcnow().isoformat(),
            "summary": summary_result
        }

    finally:
        if should_close_db:
            db.close()


async def ask_tender_question(
    tender_id: str,
    question: str,
    db: Optional[Session] = None
) -> Dict[str, Any]:
    """
    Responde dúvidas pontuais do usuário sobre o edital/termo de referência em tom objetivo e consultivo.
    """
    if not question or not question.strip():
        raise HTTPException(status_code=400, detail="A pergunta não pode estar vazia.")

    should_close_db = False
    if db is None:
        db = SessionLocal()
        should_close_db = True

    try:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            raise HTTPException(status_code=404, detail="Licitação não encontrada.")

        items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
        docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
        
        context_payload = _build_tender_context(tender, items, docs)
        user_prompt = f"""<contexto_edital>
{context_payload}
</contexto_edital>

Pergunta do licitante:
{question}
"""

        api_key = _get_api_key()
        provider = "GEMINI"
        model_name = settings.AI_MODEL or "gemini-3.0-flash"
        answer_text: Optional[str] = None

        if api_key:
            try:
                client = _cliente_gemini(api_key)

                candidate_models = list(dict.fromkeys([model_name, "gemini-3.0-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]))
                for candidate in candidate_models:
                    target_model = _normalize_model_name(candidate)
                    try:
                        async def _do_chat_call():
                            if hasattr(client.aio, "chats"):
                                chat = client.aio.chats.create(
                                    model=target_model,
                                    config={
                                        "system_instruction": SYSTEM_CHAT_INSTRUCTION
                                    }
                                )
                                send_call = chat.send_message(message=user_prompt)
                                if inspect.isawaitable(send_call):
                                    return await send_call

                            if hasattr(client.aio, "models") and hasattr(client.aio.models, "generate_content"):
                                gen_call = client.aio.models.generate_content(
                                    model=target_model,
                                    contents=user_prompt,
                                    config={
                                        "system_instruction": SYSTEM_CHAT_INSTRUCTION
                                    }
                                )
                                if inspect.isawaitable(gen_call):
                                    return await gen_call
                                return gen_call
                            return None

                        response = await _execute_with_retry_and_timeout(
                            _do_chat_call,
                            max_retries=2,
                            initial_delay=1.0,
                            timeout=30.0
                        )

                        if response is not None:
                            answer_text = response.text.strip()
                            model_name = candidate
                            break
                    except Exception as model_err:
                        logger.warning(f"Tentativa de chat com modelo {candidate} falhou ({type(model_err).__name__}): {model_err or 'timeout/erro interno'}")
                        continue
            except Exception as e:
                logger.warning(f"Erro ao chamar Google GenAI para chat ({e}). Ativando fallback consultivo.")

        if not answer_text:
            provider = "LOCAL_COMPLIANCE_ENGINE"
            model_name = "consultative-fallback-v1"
            answer_text = _generate_local_fallback_answer(tender, items, question)

        return {
            "success": True,
            "tender_id": tender.id,
            "question": question.strip(),
            "answer": answer_text,
            "provider": provider,
            "model": model_name
        }

    finally:
        if should_close_db:
            db.close()
