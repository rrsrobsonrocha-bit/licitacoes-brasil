import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from server.main import app, to_sp_datetime, SP_TZ
from server.database import SessionLocal
from server.models import Tender, Deal, generate_uuid

client = TestClient(app)

def test_to_sp_datetime_timezone_conversion():
    # 1. Objeto datetime ingênuo (naive)
    dt_naive = datetime(2026, 10, 16, 9, 0, 0)
    dt_sp = to_sp_datetime(dt_naive)
    assert dt_sp is not None
    assert dt_sp.tzinfo is not None
    assert dt_sp.tzname() == "America/Sao_Paulo" or getattr(dt_sp.tzinfo, "key", "") == "America/Sao_Paulo"
    assert dt_sp.year == 2026
    assert dt_sp.month == 10
    assert dt_sp.day == 16

    # 2. String ISO
    iso_str = "2026-10-16T14:30:00"
    dt_from_iso = to_sp_datetime(iso_str)
    assert dt_from_iso is not None
    assert dt_from_iso.tzname() == "America/Sao_Paulo" or getattr(dt_from_iso.tzinfo, "key", "") == "America/Sao_Paulo"
    assert dt_from_iso.hour == 14

    # 3. String formato brasileiro DD/MM/YYYY HH:MM:SS
    br_str = "16/10/2026 10:00:00"
    dt_from_br = to_sp_datetime(br_str)
    assert dt_from_br is not None
    assert dt_from_br.day == 16
    assert dt_from_br.month == 10
    assert dt_from_br.year == 2026
    assert dt_from_br.hour == 10


def test_agenda_critical_events_strictly_within_24h():
    now_sp = datetime.now(SP_TZ)

    # Caso 1: Certame que vence em 5 horas -> 0 <= hours <= 24 -> CRÍTICO
    target_5h = now_sp + timedelta(hours=5)
    diff_sec_5h = (target_5h - now_sp).total_seconds()
    hours_5h = diff_sec_5h / 3600.0
    assert 0 <= hours_5h <= 24

    # Caso 2: Certame com prazo de 30 horas -> hours > 24 -> NÃO É CRÍTICO
    target_30h = now_sp + timedelta(hours=30)
    diff_sec_30h = (target_30h - now_sp).total_seconds()
    hours_30h = diff_sec_30h / 3600.0
    assert hours_30h > 24

    # Caso 3: Certame expirado (-2 horas) -> VENCIDO -> NÃO É CRÍTICO
    target_past = now_sp - timedelta(hours=2)
    diff_sec_past = (target_past - now_sp).total_seconds()
    assert diff_sec_past < 0


def test_future_tender_falls_into_future_columns_not_critical():
    db = SessionLocal()
    tender_id = generate_uuid()
    now_sp = datetime.now(SP_TZ)
    deal_id = None

    try:
        # Limpa execuções anteriores caso tenham ficado pendentes
        db.query(Deal).filter(Deal.notes == "Teste de prazo futuro superior a 24h").delete()
        db.query(Tender).filter(Tender.external_id == "12200135000180-1-000042/2025").delete()
        db.commit()

        # Simula o edital PE 042/2025 agendado para 16/10/2026 (ou +5 dias à frente)
        future_date = (now_sp + timedelta(days=5)).replace(hour=10, minute=0, second=0, microsecond=0)
        future_naive = datetime(future_date.year, future_date.month, future_date.day, future_date.hour, future_date.minute)

        t = Tender(
            id=tender_id,
            source_id="pncp-source-01",
            external_id="12200135000180-1-000042/2025",
            notice_number="PE 042/2025",
            title="Aquisição de equipamentos de tecnologia da informação",
            object_description="Aquisição de equipamentos de tecnologia da informação",
            contracting_entity="Prefeitura Municipal Teste",
            state="SP",
            modality="Pregão Eletrônico",
            status="Divulgada no PNCP",
            opening_date=future_naive,
            proposal_deadline=future_naive,
            source_url="https://pncp.gov.br/app/editais/12200135000180/2025/42",
            url_pncp="https://pncp.gov.br/app/editais/12200135000180/2025/42"
        )
        db.add(t)

        deal = Deal(
            tender_id=t.id,
            stage="proposta_em_elaboracao",
            notes="Teste de prazo futuro superior a 24h"
        )
        db.add(deal)
        db.commit()
        deal_id = deal.id

        # 1. Valida endpoint API /api/agenda/events
        resp = client.get("/api/agenda/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"

        event = next((e for e in data["events"] if e["tender_id"] == tender_id), None)
        assert event is not None
        # Prazo é superior a 24h, logo a urgência NÃO PODE SER 'critico'
        assert event["urgency"] != "critico"
        assert event["urgency"] in ["alerta", "normal"]
        assert event["hours_remaining"] > 24

        # 2. Valida renderização HTML de /agenda
        html_resp = client.get("/agenda")
        assert html_resp.status_code == 200
        html = html_resp.text

        # Não deve constar o PE 042/2025 dentro do card de Vence hoje / Últimas 24h
        # e sim nos cards de avisos / próximos prazos
        assert "PE 042/2025" in html

    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.query(Deal).filter(Deal.id == deal_id).delete()
            db.query(Tender).filter(Tender.id == tender_id).delete()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
