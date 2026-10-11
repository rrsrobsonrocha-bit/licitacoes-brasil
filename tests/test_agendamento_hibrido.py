"""Agendamento híbrido do catálogo: boot em repouso, rotina diurna de 24 h e consolidação noturna com itens ME/EPP."""
import asyncio
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.database import SessionLocal
from server.main import app
from server.models import SyncProgresso, Tender, hoje_sao_paulo
from server.search_engine import agora_sao_paulo_naive
from server.services import auto_sync, pncp_compras

client = TestClient(app)
HOJE = date(2026, 10, 12)


def _as(hora: str, dia: date = HOJE) -> datetime:
    h, m = hora.split(":")
    return datetime(dia.year, dia.month, dia.day, int(h), int(m))


@pytest.fixture(autouse=True)
def estado_limpo(monkeypatch):
    monkeypatch.setattr(settings, "AUTO_SYNC_INTERVALO_MINUTOS", 120)
    monkeypatch.setattr(settings, "AUTO_SYNC_NOITE_INICIO", "02:00")
    monkeypatch.setattr(settings, "AUTO_SYNC_NOITE_FIM", "04:00")
    monkeypatch.setattr(settings, "AUTO_SYNC_DIAS", 15)
    copia = dict(auto_sync.estado)
    yield
    auto_sync.estado.clear()
    auto_sync.estado.update(copia)
    auto_sync.estado.pop("ultima_diurna", None)


def _rodar_loop(relogio, segundos=0.4):
    async def _rodar():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(auto_sync.loop_sincronizacao_automatica(atraso_inicial=0, relogio=relogio), timeout=segundos)
    asyncio.run(_rodar())


@pytest.fixture
def rotinas(monkeypatch):
    mocks = {nome: MagicMock(return_value={"tipo": nome}) for nome in ("executar_warmup", "executar_noturno", "executar_atualizacao")}
    for nome, mock in mocks.items():
        monkeypatch.setattr(auto_sync, nome, mock)
    monkeypatch.setattr(auto_sync, "catalogo_precisa_recuperacao", lambda: False)  # catálogo já carregado
    return mocks


# ---------------- Boot: repouso, sem varredura pesada ----------------
@pytest.mark.parametrize("hora_boot", ["10:00", "02:30", "01:55", "23:59"])
def test_boot_nao_dispara_varredura_e_fica_em_repouso(rotinas, monkeypatch, hora_boot):
    # Última coleta gravada há 6 h: mesmo "atrasada", nada roda no arranque
    monkeypatch.setattr(auto_sync, "ultima_coleta_registrada", lambda: datetime.utcnow() - timedelta(hours=6))
    _rodar_loop(lambda: _as(hora_boot))
    for mock in rotinas.values():
        mock.assert_not_called()
    assert auto_sync.estado["modo"] == "repouso" and auto_sync.estado["em_execucao"] is None
    assert auto_sync.estado["proxima_execucao"] and auto_sync.estado["proxima_rotina"] in ("diurna", "noturna")


def test_boot_com_catalogo_vazio_recupera_os_15_dias(rotinas, monkeypatch):
    # Banco novo/recriado/efêmero: ficar em repouso deixaria o painel zerado até a noite
    monkeypatch.setattr(auto_sync, "catalogo_precisa_recuperacao", lambda: True)
    monkeypatch.setattr(auto_sync, "ultima_coleta_registrada", lambda: None)
    _rodar_loop(lambda: _as("10:00"))
    rotinas["executar_warmup"].assert_called_once_with()
    rotinas["executar_atualizacao"].assert_not_called()
    assert auto_sync.estado["proxima_rotina"] == "diurna"  # depois da recuperação, volta à rotina normal


def test_criterio_de_recuperacao(monkeypatch):
    monkeypatch.setattr(auto_sync, "contar_vigentes", lambda: 103)
    monkeypatch.setattr(settings, "AUTO_SYNC_MIN_VIGENTES", 500)
    assert auto_sync.catalogo_precisa_recuperacao() is True
    monkeypatch.setattr(auto_sync, "contar_vigentes", lambda: 15300)
    assert auto_sync.catalogo_precisa_recuperacao() is False


def test_boot_agenda_proxima_diurna_e_respeita_repouso_minimo(rotinas, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_SYNC_REPOUSO_ARRANQUE_MINUTOS", 15)
    monkeypatch.setattr(auto_sync, "ultima_coleta_registrada", lambda: None)
    _rodar_loop(lambda: _as("10:00"))
    assert auto_sync.estado["proxima_rotina"] == "diurna"
    proxima = auto_sync._utc_para_brasilia(datetime.fromisoformat(auto_sync.estado["proxima_execucao"]))
    assert abs((proxima - _as("12:00")).total_seconds()) < 5  # 2 h depois do boot das 10:00


def test_loop_executa_diurna_quando_vence_e_noturna_na_janela(rotinas, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_SYNC_REPOUSO_ARRANQUE_MINUTOS", 0)
    # Última coleta gravada às 09:00 (no relógio simulado): a diurna vence às 11:00
    monkeypatch.setattr(auto_sync, "ultima_coleta_registrada",
                        lambda: datetime.utcnow() - (agora_sao_paulo_naive() - _as("09:00")))
    horarios = iter([_as("01:00"), _as("12:00"), _as("12:00")] + [_as("02:10", HOJE + timedelta(days=1))] * 50)
    ultimo = {"t": _as("01:00")}

    def relogio():
        ultimo["t"] = next(horarios, ultimo["t"])
        return ultimo["t"]

    _rodar_loop(relogio, segundos=0.5)
    rotinas["executar_atualizacao"].assert_called_once()  # às 12:00 a diurna (09:00 + 2 h) estava vencida
    rotinas["executar_noturno"].assert_called_once()  # 02:10 do dia seguinte: uma consolidação por noite
    rotinas["executar_warmup"].assert_not_called()


# ---------------- Calendário (horário de Brasília) ----------------
@pytest.mark.parametrize("agora,ultima,noite,esperado", [
    ("10:00", "09:00", None, ("diurna", "11:00")),
    ("10:00", None, None, ("diurna", "12:00")),
    ("23:30", "23:00", None, ("diurna", "01:00+1")),
    ("01:00", "00:30", None, ("noturna", "02:00")),        # diurna cairia às 02:30, dentro da janela
    ("02:30", "01:00", None, ("noturna", "02:30")),        # dentro da janela e sem consolidação hoje
    ("02:30", "01:00", "hoje", ("diurna", "04:00")),       # já consolidou: a diurna espera o fim da janela
    ("05:00", "04:00", "hoje", ("diurna", "06:00")),
])
def test_proxima_rotina(agora, ultima, noite, esperado):
    def _t(h):
        if h is None:
            return None
        return _as(h[:5], HOJE + timedelta(days=1)) if h.endswith("+1") else _as(h)

    tipo, quando = auto_sync.proxima_rotina(_t(agora), _t(ultima), HOJE if noite == "hoje" else None)
    assert (tipo, quando) == (esperado[0], _t(esperado[1]))


# ---------------- Botão manual: estritamente 24 h ----------------
def test_botao_manual_coleta_somente_as_ultimas_24h(monkeypatch):
    chamadas = []
    concluiu = threading.Event()

    def coletar(dias, notificar_alertas, tipo, consolidar_desde=None):
        chamadas.append((dias, notificar_alertas, tipo, consolidar_desde))
        return {"tipo": tipo}

    monkeypatch.setattr(auto_sync, "_coletar_dias", coletar)
    monkeypatch.setattr(auto_sync, "enriquecer_itens", lambda **kw: (concluiu.set(), {"analisados": 0})[1])
    assert auto_sync.disparar_em_background() is True
    assert concluiu.wait(5)
    for _ in range(50):
        if not auto_sync._trava.locked():
            break
        time.sleep(0.05)

    hoje = hoje_sao_paulo()
    assert chamadas == [([hoje.strftime("%Y%m%d"), (hoje - timedelta(days=1)).strftime("%Y%m%d")], True, "diurna", None)]

    # Durante a consolidação noturna o botão não inicia outra coleta
    auto_sync._trava.acquire()
    try:
        assert auto_sync.disparar_em_background() is False
    finally:
        auto_sync._trava.release()


def test_rotina_diurna_le_itens_dos_editais_novos(monkeypatch):
    marcador = f"NOVO{uuid.uuid4().hex[:6]}"
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    try:
        base = dict(source_id="pncp-source-01", object_description="Objeto", contracting_entity="Órgão", state="SP",
                    status="Divulgada no PNCP", source_url="https://pncp.gov.br", proposal_deadline=agora + timedelta(days=5))
        antigo = Tender(external_id=f"{marcador}-antigo", title="Antigo", created_at=datetime.utcnow() - timedelta(days=2), **base)
        db.add(antigo)
        db.commit()
        antigo_id = antigo.id
    finally:
        db.close()

    lidos = []

    async def sincronizar_itens(sessao, tender, forcar=False):
        lidos.append(tender.external_id)
        tender.itens_exclusivos_me_epp, tender.itens_ampla_concorrencia = 1, 0
        sessao.commit()
        return {"status": "sincronizado"}

    def coletar(dias, notificar_alertas, tipo, consolidar_desde=None):
        sessao = SessionLocal()
        try:
            sessao.add(Tender(external_id=f"{marcador}-novo", title="Novo", **base))
            sessao.commit()
        finally:
            sessao.close()
        return {"tipo": tipo, "novos": 1}

    monkeypatch.setattr(auto_sync, "_coletar_dias", coletar)
    monkeypatch.setattr(pncp_compras, "sincronizar_itens", sincronizar_itens)
    try:
        resumo = auto_sync.executar_atualizacao()
        assert lidos == [f"{marcador}-novo"]  # só o edital recém-capturado; o acervo fica para a noite
        assert resumo["itens"]["analisados"] == 1
        db = SessionLocal()
        try:
            assert db.get(Tender, antigo_id).itens_exclusivos_me_epp is None
        finally:
            db.close()
    finally:
        db = SessionLocal()
        db.query(Tender).filter(Tender.external_id.like(f"{marcador}%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ---------------- Rotina noturna: 15 dias + fila de itens ----------------
def test_rotina_noturna_consolida_os_15_dias_e_processa_a_fila_de_itens(monkeypatch):
    chamadas, filas = [], []

    def coletar(dias, notificar_alertas, tipo, consolidar_desde=None):
        chamadas.append((dias, notificar_alertas, tipo, consolidar_desde))
        assert auto_sync.estado["em_execucao"] == "noturna"
        return {"tipo": tipo}

    def enriquecer(**kw):
        filas.append(kw)
        return {"analisados": 7, "sem_itens": 1, "pendentes": 0}

    monkeypatch.setattr(auto_sync, "_coletar_dias", coletar)
    monkeypatch.setattr(auto_sync, "enriquecer_itens", enriquecer)
    fim = agora_sao_paulo_naive() + timedelta(hours=1)
    resumo = auto_sync.executar_noturno(fim)

    hoje = hoje_sao_paulo()
    dias, notificar, tipo, desde = chamadas[0]
    assert dias == [(hoje - timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
    assert notificar is False and tipo == "noturna" and desde is not None
    assert resumo["itens"]["analisados"] == 7
    # A fila de itens é limitada à janela: para quando o relógio passa do fim
    parar = filas[0]["parar"]
    assert parar() is False and "criados_desde" not in filas[0]
    assert auto_sync.estado["modo"] == "repouso" and auto_sync.estado["em_execucao"] is None


def test_consolidacao_rele_dias_ja_concluidos():
    dia = (hoje_sao_paulo() - timedelta(days=9)).strftime("%Y%m%d")
    db = SessionLocal()
    try:
        db.query(SyncProgresso).filter(SyncProgresso.dia == dia).delete()
        for codigo in auto_sync.MODALIDADES_DISPUTA:
            db.add(SyncProgresso(dia=dia, codigo_modalidade=codigo, ultima_pagina=4, total_paginas=4, concluido=True))
        db.commit()
    finally:
        db.close()
    try:
        assert auto_sync.plano_de_retomada(dia) == {}  # coleta comum: dia encerrado e concluído é pulado
        noite = datetime.utcnow() + timedelta(seconds=5)
        assert auto_sync.plano_de_retomada(dia, consolidar_desde=noite) == {6: 1, 8: 1}  # noturna relê tudo
    finally:
        db = SessionLocal()
        db.query(SyncProgresso).filter(SyncProgresso.dia == dia).delete()
        db.commit()
        db.close()


def test_fila_de_itens_para_no_fim_da_janela(monkeypatch):
    marcador = f"FILA{uuid.uuid4().hex[:6]}"
    agora = agora_sao_paulo_naive()
    desde = datetime.utcnow()
    db = SessionLocal()
    try:
        for n in range(3):
            db.add(Tender(source_id="pncp-source-01", external_id=f"{marcador}-{n}", title="T", object_description="O",
                          contracting_entity="Ó", state="SP", status="Divulgada no PNCP", source_url="https://pncp.gov.br",
                          proposal_deadline=agora + timedelta(days=1, minutes=n)))
        db.commit()
    finally:
        db.close()
    lidos = []

    async def sincronizar_itens(sessao, tender, forcar=False):
        lidos.append(tender.external_id)
        return {"status": "vazio"}

    monkeypatch.setattr(pncp_compras, "sincronizar_itens", sincronizar_itens)
    try:
        resumo = auto_sync.enriquecer_itens(criados_desde=desde,
                                            parar=lambda: len(lidos) >= 2)
        assert lidos == [f"{marcador}-0", f"{marcador}-1"] and resumo["pendentes"] == 1
    finally:
        auto_sync._itens_sem_resultado.clear()
        db = SessionLocal()
        db.query(Tender).filter(Tender.external_id.like(f"{marcador}%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ---------------- Indicador do cabeçalho ----------------
def test_rotulos_do_status_no_cabecalho():
    agora_utc = datetime.utcnow()
    auto_sync.estado.update(em_execucao=None, modo="repouso", ultima_execucao=agora_utc.isoformat(),
                            proxima_execucao=(agora_utc + timedelta(hours=2)).isoformat())
    dados = client.get("/api/sync/status").json()
    ultima = agora_sao_paulo_naive().strftime("%H:%M")
    proxima = (agora_sao_paulo_naive() + timedelta(hours=2)).strftime("%H:%M")
    assert dados["rotulo"] == f"Atualizado às {ultima} · Próxima verificação às {proxima}"
    assert dados["janela_noturna"] == "02:00-04:00" and "ultima_diurna" not in dados

    auto_sync.estado["em_execucao"] = "diurna"
    assert client.get("/api/sync/status").json()["rotulo"] == "Sincronizando últimas 24h..."
    auto_sync.estado["em_execucao"] = "warmup"
    assert client.get("/api/sync/status").json()["rotulo"] == "Recuperando o catálogo (últimos 15 dias)..."
    auto_sync.estado["em_execucao"] = "noturna"
    assert client.get("/api/sync/status").json()["rotulo"] == "Consolidação noturna (15 dias + itens) em andamento"
