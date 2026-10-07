import pytest
from server.search_engine import normalize_text, parse_search_query, calculate_relevance_score
from server.database import SessionLocal
from server.models import Tender, Source
from server.schemas import SearchFilterParams
from server.search_engine import search_tenders

def test_text_normalization():
    # Deve remover acentos, converter maiúsculas e manter conteúdo semântico
    assert normalize_text("Licitação Pública") == "licitacao publica"
    assert normalize_text("Remédio & Farmácia") == "remedio & farmacia"
    assert normalize_text("SERINGAS DESCARTÁVEIS") == "seringas descartaveis"
    assert normalize_text("  Espaços   Extras ") == "espacos   extras"

def test_query_parsing():
    # Expressão exata entre aspas
    exact, terms = parse_search_query('"materiais médico-hospitalares" dipirona')
    assert "materiais medico-hospitalares" in exact
    assert "dipirona" in terms

    # Apenas termos soltos
    exact2, terms2 = parse_search_query("medicamentos alagoas sesau")
    assert len(exact2) == 0
    assert "medicamentos" in terms2
    assert "alagoas" in terms2

def test_search_alagoas_healthcare_scenario():
    """Testa o caso específico requerido pelo usuário: Medicamentos e farmácias em Alagoas"""
    db = SessionLocal()
    try:
        params = SearchFilterParams(
            q="medicamentos farmácias",
            state="AL",
            category="Medicamentos"
        )
        results = search_tenders(db, params)
        assert results.total >= 1
        found = results.results[0]
        assert found.state == "AL"
        assert "Medicamentos" in found.category
        assert "medicamentos" in found.title.lower() or "medicamentos" in found.object_description.lower()
    finally:
        db.close()

def test_search_value_range_filter():
    db = SessionLocal()
    try:
        params = SearchFilterParams(
            value_min=4000000.0,
            value_max=5000000.0
        )
        results = search_tenders(db, params)
        for item in results.results:
            assert item.estimated_total_value >= 4000000.0
            assert item.estimated_total_value <= 5000000.0
    finally:
        db.close()

def test_export_tenders_csv_endpoint():
    from fastapi.testclient import TestClient
    from server.main import app

    client = TestClient(app)
    res = client.get("/api/tenders/export/csv?state=AL&q=medicamentos")
    assert res.status_code == 200
    assert "text/csv" in res.headers.get("content-type", "")
    assert "attachment" in res.headers.get("content-disposition", "")
    text_content = res.content.decode("utf-8-sig")
    lines = text_content.strip().split("\n")
    assert len(lines) >= 2
    assert "objeto;orgao;estado;valor_estimado;data_abertura;url_disputa;url_pncp" in lines[0]

