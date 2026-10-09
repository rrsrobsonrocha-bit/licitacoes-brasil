import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from server.main import app
from server.database import SessionLocal
from server.models import Tender, AIAnalysis
from server.services.ai_service import analyze_tender_summary, ask_tender_question

client = TestClient(app)


@pytest.fixture
def sample_tender():
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None, "Banco de testes deve possuir ao menos um certame semeado"
        return tender
    finally:
        db.close()


def test_ai_summary_endpoint_success(sample_tender):
    """Testa o endpoint POST /api/tenders/{tender_id}/ai-summary gerando o resumo estruturado"""
    response = client.post(f"/api/tenders/{sample_tender.id}/ai-summary")
    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["tender_id"] == sample_tender.id
    assert "summary" in data

    summary = data["summary"]
    # Validação das 4 dimensões obrigatórias do Resumo Executivo
    assert "objeto_resumido" in summary
    assert "habilitacao_tecnica" in summary
    assert "prazos_entrega_execucao" in summary
    assert "condicoes_pagamento_sancoes" in summary

    assert len(summary["objeto_resumido"]) > 10
    assert isinstance(summary["habilitacao_tecnica"], list)
    assert len(summary["habilitacao_tecnica"]) >= 1


def test_ai_summary_caching_behavior(sample_tender):
    """Testa se a segunda chamada do resumo recupera os dados persistidos em cache"""
    # Primeira chamada (garantir cache existente)
    resp1 = client.post(f"/api/tenders/{sample_tender.id}/ai-summary")
    assert resp1.status_code == 200

    # Segunda chamada imediata sem force_refresh
    resp2 = client.post(f"/api/tenders/{sample_tender.id}/ai-summary")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["cached"] is True
    assert data2["summary"]["objeto_resumido"] == resp1.json()["summary"]["objeto_resumido"]

    # Chamada com force_refresh deve reprocessar
    resp3 = client.post(f"/api/tenders/{sample_tender.id}/ai-summary?force_refresh=true")
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["cached"] is False


def test_ai_summary_not_found():
    """Valida retorno 404 para ID de certame inexistente"""
    response = client.post("/api/tenders/uuid-inexistente-9999/ai-summary")
    assert response.status_code == 404
    assert "não encontrada" in response.json()["detail"].lower()


def test_ai_chat_endpoint_success(sample_tender):
    """Testa o endpoint POST /api/tenders/{tender_id}/ai-chat ('Converse com o Edital')"""
    response = client.post(
        f"/api/tenders/{sample_tender.id}/ai-chat",
        json={"question": "Qual é o prazo limite para envio das propostas e abertura?"}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["tender_id"] == sample_tender.id
    assert "prazo" in data["question"].lower()
    assert "answer" in data
    assert len(data["answer"]) > 20
    assert "model" in data


def test_ai_chat_consultative_question_impugnacao(sample_tender):
    """Testa questionamento consultivo sobre impugnação com referência à Lei 14.133/2021"""
    response = client.post(
        f"/api/tenders/{sample_tender.id}/ai-chat",
        json={"question": "Qual o prazo para impugnar o edital?"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "answer" in data
    answer_text = data["answer"]
    assert "14.133" in answer_text or "impugna" in answer_text.lower() or "dias úteis" in answer_text.lower()


def test_ai_chat_empty_question(sample_tender):
    """Valida erro 400 ou 422 para pergunta vazia"""
    response = client.post(
        f"/api/tenders/{sample_tender.id}/ai-chat",
        json={"question": "   "}
    )
    assert response.status_code in (400, 422)


def test_ai_chat_tender_not_found():
    """Valida erro 404 para ID inexistente no chat"""
    response = client.post(
        "/api/tenders/uuid-inexistente-9999/ai-chat",
        json={"question": "Qual o valor?"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_direct_service_mock_gemini(sample_tender):
    """Testa o serviço direto ai_service com mock do SDK oficial do Google Gemini"""
    mock_summary_json = {
        "objeto_resumido": "Contratação direta simulada para testes automatizados.",
        "habilitacao_tecnica": [
            "Atestado de capacidade técnica com 50% do quantitativo.",
            "Registro no CREA/CRA."
        ],
        "prazos_entrega_execucao": "Até 15 dias após emissão da ordem de serviço.",
        "condicoes_pagamento_sancoes": "Pagamento em 30 dias. Multa de 10% por mora.",
        "pontos_atencao": ["Exigência de amostra no lote 1."]
    }

    mock_response = MagicMock()
    mock_response.text = json_dumps = __import__("json").dumps(mock_summary_json)

    with patch("google.genai.Client") as MockGenaiClient:
        mock_instance = MockGenaiClient.return_value
        mock_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)

        result = await analyze_tender_summary(
            tender_id=sample_tender.id,
            force_refresh=True
        )

        assert result["success"] is True
        assert result["summary"]["objeto_resumido"] == mock_summary_json["objeto_resumido"]
        assert len(result["summary"]["habilitacao_tecnica"]) == 2


@pytest.mark.asyncio
async def test_direct_service_chat_mock(sample_tender):
    """Testa a função direta ask_tender_question com resposta mockada do Gemini"""
    mock_response = MagicMock()
    mock_response.text = "O prazo de impugnação é de até 3 dias úteis antes da abertura conforme art. 164 da Lei 14.133/2021."

    with patch("google.genai.Client") as MockGenaiClient:
        mock_instance = MockGenaiClient.return_value
        mock_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)

        result = await ask_tender_question(
            tender_id=sample_tender.id,
            question="Qual o prazo de impugnação?"
        )

        assert result["success"] is True
        assert "art. 164" in result["answer"]
