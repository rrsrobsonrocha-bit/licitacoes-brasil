"""Certames coletados pertencem à fonte oficial do PNCP; a antiga fonte "Hub Multifonte" é migrada e removida."""
import asyncio
import uuid
from unittest.mock import patch

from server.database import SessionLocal
from server.migrate import DOMINIO_HUB_MULTIFONTE, _migrar_hub_multifonte_para_pncp
from server.models import Source, Tender
from server.seed_data import ID_FONTE_PNCP
from server.services.portal_collector import PortalCollectorService
from tests.pncp_simulado import registro_pncp, resposta_httpx_pncp


def test_hub_multifonte_tem_certames_reatribuidos_ao_pncp_e_e_removido():
    db = SessionLocal()
    marcador = f"HUB-{uuid.uuid4().hex[:8]}"
    try:
        hub = Source(name="Hub Multifonte Licitações Brasil", base_url=f"https://{DOMINIO_HUB_MULTIFONTE}",
                     official_domain=DOMINIO_HUB_MULTIFONTE, source_type="AGREGADOR_MULTIFONTE",
                     integration_type="API_OFICIAL", active=True)
        db.add(hub)
        db.flush()
        ids = []
        for n in range(3):
            t = Tender(source_id=hub.id, external_id=f"{marcador}-{n}", title=f"Edital {marcador} {n}",
                       object_description="Objeto", contracting_entity="Órgão", state="DF",
                       source_url="https://pncp.gov.br")
            db.add(t)
            db.flush()
            ids.append(t.id)
        db.commit()
        total_antes = db.query(Tender).count()

        _migrar_hub_multifonte_para_pncp()
        _migrar_hub_multifonte_para_pncp()  # idempotente

        db.expire_all()
        assert db.query(Tender).count() == total_antes  # nenhum certame apagado em cascata
        assert {t.source_id for t in db.query(Tender).filter(Tender.id.in_(ids))} == {ID_FONTE_PNCP}
        assert db.query(Source).filter(Source.official_domain == DOMINIO_HUB_MULTIFONTE).count() == 0
    finally:
        db.query(Tender).filter(Tender.external_id.like(f"{marcador}%")).delete(synchronize_session=False)
        db.query(Source).filter(Source.official_domain == DOMINIO_HUB_MULTIFONTE).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_sync_pipeline_grava_na_fonte_oficial_do_pncp():
    registro = registro_pncp(9900 + uuid.uuid4().int % 99, federal=True)
    db = SessionLocal()
    try:
        with patch("httpx.AsyncClient.get", return_value=resposta_httpx_pncp([registro])):
            resultado = asyncio.run(PortalCollectorService().sync_pipeline(sources=["PNCP"], db_session=db))
        assert not resultado["errors"]
        tender = db.query(Tender).filter(Tender.external_id == registro["numeroControlePNCP"]).one()
        assert tender.source_id == ID_FONTE_PNCP
        assert db.query(Source).filter(Source.official_domain == DOMINIO_HUB_MULTIFONTE).count() == 0
    finally:
        db.query(Tender).filter(Tender.external_id == registro["numeroControlePNCP"]).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_user_agents_sem_dominio_ficticio():
    from server.connectors.pncp import PNCPConnector
    from server.services import pncp_compras, portal_collector

    agentes = [
        PNCPConnector().session.headers["User-Agent"],
        pncp_compras.CABECALHOS["User-Agent"],
        portal_collector.DEFAULT_HEADERS["User-Agent"],
    ]
    assert all(DOMINIO_HUB_MULTIFONTE not in a for a in agentes)
