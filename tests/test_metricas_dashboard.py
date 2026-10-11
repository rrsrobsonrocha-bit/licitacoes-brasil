"""Métricas globais do painel: contam o catálogo inteiro, sem filtro por usuário, cache ou janela de 24 h."""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

from server.database import SessionLocal
from server.main import app
from server.models import Tender
from server.search_engine import agora_sao_paulo_naive, filtro_vigentes

client = TestClient(app)


@pytest.fixture
def catalogo():
    marcador = f"MET{uuid.uuid4().hex[:6]}"
    agora = agora_sao_paulo_naive()
    base = dict(source_id="pncp-source-01", title="Edital", object_description="Objeto", state="SP",
                status="Divulgada no PNCP", source_url="https://pncp.gov.br")
    linhas = [
        # publicado hoje, vigente
        dict(contracting_entity=f"Órgão A {marcador}", publication_date=agora, proposal_deadline=agora + timedelta(days=3)),
        # publicado há 60 dias, ainda vigente (ciclo longo): conta como vigente, não como "hoje"
        dict(contracting_entity=f"Órgão A {marcador}", publication_date=agora - timedelta(days=60), proposal_deadline=agora + timedelta(days=1)),
        # sem encerramento, abertura futura: vigente
        dict(contracting_entity=f"Órgão B {marcador}", publication_date=agora - timedelta(days=5), opening_date=agora + timedelta(days=2)),
        # encerrado: só entra no total
        dict(contracting_entity=f"Órgão C {marcador}", publication_date=agora - timedelta(days=20), proposal_deadline=agora - timedelta(days=1)),
    ]
    db = SessionLocal()
    try:
        for n, campos in enumerate(linhas):
            db.add(Tender(external_id=f"{marcador}-{n}", **base, **campos))
        db.commit()
    finally:
        db.close()
    yield marcador
    db = SessionLocal()
    db.query(Tender).filter(Tender.external_id.like(f"{marcador}%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _esperado():
    db = SessionLocal()
    try:
        inicio_hoje = datetime.combine(agora_sao_paulo_naive().date(), datetime.min.time())
        return {
            "total_tenders_count": db.query(func.count(Tender.id)).scalar(),
            "vigentes_count": db.query(func.count(Tender.id)).filter(filtro_vigentes()).scalar(),
            "companies_count": db.query(func.count(func.distinct(Tender.contracting_entity))).filter(filtro_vigentes()).scalar(),
            "tenders_today_count": db.query(func.count(Tender.id)).filter(
                Tender.publication_date >= inicio_hoje, Tender.publication_date < inicio_hoje + timedelta(days=1)).scalar(),
        }
    finally:
        db.close()


def test_metricas_do_painel_contam_o_catalogo_global(catalogo, criar_usuario):
    esperado = _esperado()
    assert esperado["total_tenders_count"] >= 4 and esperado["vigentes_count"] >= 3

    # Visitante, conta nova sem nenhum negócio e outra conta: os mesmos números do catálogo global
    contas = [None, criar_usuario("Conta Nova Métricas"), criar_usuario("Outra Conta Métricas")]
    for conta in contas:
        dados = client.get("/api/user/dashboard-summary", headers=conta.headers if conta else None).json()
        for chave, valor in esperado.items():
            assert dados[chave] == valor, (chave, conta)

    html = client.get("/dashboard", cookies=contas[1].cookies).text
    assert f'id="stat-total-tenders" class="font-mono">{esperado["total_tenders_count"]}<' in html


def test_metricas_acompanham_o_banco_sem_cache(catalogo):
    antes = client.get("/api/user/dashboard-summary").json()
    db = SessionLocal()
    try:
        agora = agora_sao_paulo_naive()
        db.add(Tender(external_id=f"{catalogo}-extra", source_id="pncp-source-01", title="Extra", object_description="O",
                      contracting_entity=f"Órgão Novo {catalogo}", state="RJ", status="Divulgada no PNCP",
                      source_url="https://pncp.gov.br", publication_date=agora, proposal_deadline=agora + timedelta(days=9)))
        db.commit()
    finally:
        db.close()
    depois = client.get("/api/user/dashboard-summary").json()
    assert depois["total_tenders_count"] == antes["total_tenders_count"] + 1
    assert depois["vigentes_count"] == antes["vigentes_count"] + 1
    assert depois["companies_count"] == antes["companies_count"] + 1
    assert depois["tenders_today_count"] == antes["tenders_today_count"] + 1


def test_health_check_do_banco_mostra_tipo_e_volume_do_catalogo(catalogo):
    dados = client.get("/healthz/db").json()
    esperado = _esperado()
    assert dados["database"] == "connected" and dados["engine"] == "sqlite"
    assert dados["editais"] == esperado["total_tenders_count"]
    assert dados["editais_vigentes"] == esperado["vigentes_count"]
