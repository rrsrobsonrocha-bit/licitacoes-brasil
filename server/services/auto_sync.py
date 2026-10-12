"""
Abastecimento contínuo do catálogo global de editais (Licitações Brasil).

O catálogo é compartilhado por todas as contas e permanente: esta rotina só insere ou atualiza
certames devolvidos pela API oficial do PNCP (upsert por numeroControlePNCP) e nunca apaga nada.
Sem resposta da API, nada é gravado.

Política híbrida (horário de Brasília):
- Arranque (deploy/reinício): o servidor sobe em repouso. Nunca dispara a varredura de 15 dias nem
  coleta logo de cara: agenda a próxima rotina diurna para 2 h após a última coleta gravada no banco
  (no mínimo ``AUTO_SYNC_REPOUSO_ARRANQUE_MINUTOS`` depois do boot). Um deploy dentro da janela
  noturna não consolida naquela noite; a consolidação volta na noite seguinte.
  Exceção (rede de segurança): se o banco chegar sem catálogo (menos de ``AUTO_SYNC_MIN_VIGENTES``
  vigentes: banco novo, recriado ou efêmero), o boot recupera os últimos 15 dias na hora, com retomada.
- Rotina diurna (fora da janela noturna), a cada ``AUTO_SYNC_INTERVALO_MINUTOS`` (120): só as
  últimas 24 h (hoje e ontem), com alertas, e em seguida os itens oficiais dos editais novos
  vigentes, que já nascem com o filtro ME/EPP. O botão "Sincronizar agora" roda exatamente isto.
- Rotina noturna (``AUTO_SYNC_NOITE_INICIO``-``AUTO_SYNC_NOITE_FIM``, 02h-04h): relê os últimos
  ``AUTO_SYNC_DIAS`` (15) dias inteiros para consolidar situação, cancelamentos e retificações e,
  em paralelo, processa a fila de itens dos editais vigentes ainda não analisados até o fim da janela.
- Retomada: o progresso de cada dia/modalidade fica em ``sync_progresso`` (coleta diurna e carga
  manual retomam de onde pararam). A consolidação noturna relê todos os dias desde a página 1.
- Leitura sob demanda: abrir um edital no drawer lê os itens na hora (ver pncp_compras).
"""
import asyncio
import gc
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func

from server.config import settings
from server.database import SessionLocal
from server.integrations.opportunity_collector import sync_procurement_opportunities
from server.models import SyncProgresso, Tender, hoje_sao_paulo
from server.search_engine import agora_sao_paulo_naive, filtro_vigentes
from server.services import pncp_compras

# Modalidades com disputa: 6 = Pregão Eletrônico, 8 = Dispensa de Licitação
MODALIDADES_DISPUTA = [6, 8]

_trava = threading.Lock()
estado: Dict[str, Any] = {
    "modo": "repouso",  # repouso, diurna ou noturna
    "proxima_rotina": None,
    "em_execucao": None,
    "progresso": None,
    "ultima_execucao": None,
    "ultimo_resultado": None,
    "proxima_execucao": None,
}


def contar_vigentes() -> int:
    db = SessionLocal()
    try:
        return db.query(func.count(Tender.id)).filter(filtro_vigentes()).scalar() or 0
    finally:
        db.close()


def em_producao() -> bool:
    return settings.DATABASE_URL.startswith(("postgres://", "postgresql"))


def _progresso_salvo(dia: str) -> Dict[int, SyncProgresso]:
    db = SessionLocal()
    try:
        registros = db.query(SyncProgresso).filter(SyncProgresso.dia == dia).all()
        for r in registros:
            db.expunge(r)
        return {r.codigo_modalidade: r for r in registros}
    finally:
        db.close()


def _salvar_progresso(dia: str, codigo_modalidade: int, pagina: int, total_paginas: int, concluido: bool) -> None:
    db = SessionLocal()
    try:
        registro = db.query(SyncProgresso).filter(
            SyncProgresso.dia == dia, SyncProgresso.codigo_modalidade == codigo_modalidade
        ).first()
        if not registro:
            registro = SyncProgresso(dia=dia, codigo_modalidade=codigo_modalidade)
            db.add(registro)
        registro.ultima_pagina = max(0, pagina)
        registro.total_paginas = total_paginas or 0
        registro.concluido = bool(concluido)
        db.commit()
    except Exception as exc:  # o progresso é auxiliar: nunca interrompe a coleta
        db.rollback()
        print(f"[AUTO-SYNC] Não foi possível salvar o progresso de {dia}/{codigo_modalidade}: {exc!r}")
    finally:
        db.close()


def plano_de_retomada(dia: str, consolidar_desde: Optional[datetime] = None) -> Dict[int, int]:
    """
    Página inicial de cada modalidade do dia; modalidades ausentes do resultado já foram concluídas.
    Dias encerrados (anteriores a ontem) não mudam mais no PNCP: concluídos são pulados e os
    interrompidos seguem da próxima página. Hoje e ontem relêem a última página lida.
    Com ``consolidar_desde`` (UTC, início da consolidação noturna), o progresso anterior a esse
    instante é ignorado: o dia é relido inteiro para capturar situação, cancelamentos e retificações.
    """
    salvos = _progresso_salvo(dia)
    encerrado = dia < (hoje_sao_paulo() - timedelta(days=1)).strftime("%Y%m%d")
    plano: Dict[int, int] = {}
    for codigo in MODALIDADES_DISPUTA:
        registro = salvos.get(codigo)
        if registro and consolidar_desde and registro.atualizado_em < consolidar_desde:
            registro = None
        if not registro:
            plano[codigo] = 1
        elif encerrado:
            if not registro.concluido:
                plano[codigo] = registro.ultima_pagina + 1
        else:
            plano[codigo] = max(1, registro.ultima_pagina)
    return plano


def _coletar_dias(dias: List[str], notificar_alertas: bool, tipo: str,
                  consolidar_desde: Optional[datetime] = None) -> Dict[str, Any]:
    """Coleta cada dia (YYYYMMDD) em sessão própria. Um dia com falha não interrompe os demais."""
    inicio = datetime.utcnow()
    resumo = {"tipo": tipo, "dias": len(dias), "dias_ja_concluidos": 0, "coletados": 0, "novos": 0,
              "atualizados": 0, "falhas": []}
    try:
        for indice, dia in enumerate(dias, start=1):
            plano = plano_de_retomada(dia, consolidar_desde)
            if not plano:
                resumo["dias_ja_concluidos"] += 1
                continue

            def _progresso(pagina: Dict[str, Any], dia=dia, indice=indice) -> None:
                _salvar_progresso(dia, pagina["codigo_modalidade"], pagina["pagina"],
                                  pagina["total_paginas"], pagina["concluido"])
                estado["progresso"] = {
                    "tipo": tipo,
                    "dia": f"{dia[6:8]}/{dia[4:6]}/{dia[:4]}",
                    "dia_indice": indice,
                    "total_dias": len(dias),
                    "modalidade": pagina["modalidade"],
                    "pagina": pagina["pagina"],
                    "total_paginas": pagina["total_paginas"],
                    "coletados": resumo["coletados"] + pagina["coletados"],
                    "novos": resumo["novos"] + pagina["novos"],
                    "atualizado_em": datetime.utcnow().isoformat(),
                }

            db = SessionLocal()
            try:
                r = sync_procurement_opportunities(
                    db=db,
                    data_inicial=dia,
                    data_final=dia,
                    modalidades=[c for c in MODALIDADES_DISPUTA if c in plano],
                    pagina_inicial=plano,
                    paginas_por_modalidade=settings.AUTO_SYNC_PAGINAS_POR_DIA,
                    limit_per_page=50,
                    reforco_al=False,
                    notificar_alertas=notificar_alertas,
                    ao_progredir=_progresso,
                )
                resumo["coletados"] += r.get("total_coletados", 0)
                resumo["novos"] += r.get("novos_inseridos", 0)
                resumo["atualizados"] += r.get("atualizados", 0)
                resumo["falhas"].extend(f"{dia}: {erro}" for erro in r.get("erros", []))
                print(f"[AUTO-SYNC] {tipo} {dia}: {r.get('total_coletados', 0)} coletados, "
                      f"{r.get('novos_inseridos', 0)} novos (catálogo: {r.get('total_geral_banco', 0)}).")
            except Exception as exc:
                db.rollback()
                resumo["falhas"].append(f"{dia}: {exc}")
                print(f"[AUTO-SYNC] {tipo} {dia}: falha ({exc!r}); seguindo para o próximo dia.")
            finally:
                db.close()
        resumo["vigentes"] = contar_vigentes()
        resumo["duracao_segundos"] = round((datetime.utcnow() - inicio).total_seconds(), 1)
        return resumo
    finally:
        estado["progresso"] = None




def _ultimos_dias(quantidade: int) -> List[str]:
    hoje = hoje_sao_paulo()
    return [(hoje - timedelta(days=i)).strftime("%Y%m%d") for i in range(max(1, quantidade))]


# ------------------------------------------------------------------------------
# Itens oficiais (benefício ME/EPP)
# ------------------------------------------------------------------------------
# tender_id -> instante da última tentativa sem resultado (compra sem itens publicados ou PNCP fora do ar)
_itens_sem_resultado: Dict[str, float] = {}
ESPERA_NOVA_TENTATIVA_ITENS = 6 * 3600


def enriquecer_itens(*, criados_desde: Optional[datetime] = None, limite: Optional[int] = None,
                     parar: Optional[Callable[[], bool]] = None) -> Dict[str, int]:
    """
    Lê no PNCP os itens dos editais vigentes ainda sem análise de benefício ME/EPP, prazo mais
    próximo primeiro. ``criados_desde`` (UTC) restringe aos editais recém-capturados; ``parar``
    encerra a fila (ex.: fim da janela noturna). Ritmo: PNCP_ITENS_RATE_LIMIT_PER_MINUTE.
    """
    resumo = {"analisados": 0, "sem_itens": 0, "pendentes": 0}
    taxa = settings.PNCP_ITENS_RATE_LIMIT_PER_MINUTE
    pausa = 60.0 / taxa if taxa > 0 else 0.0
    agora = time.time()
    db = SessionLocal()
    try:
        consulta = db.query(Tender.id).filter(filtro_vigentes(), Tender.itens_exclusivos_me_epp.is_(None))
        if criados_desde is not None:
            consulta = consulta.filter(Tender.created_at >= criados_desde)
        ids = [tid for (tid,) in consulta.order_by(Tender.proposal_deadline.asc()).all()
               if agora - _itens_sem_resultado.get(tid, 0) > ESPERA_NOVA_TENTATIVA_ITENS]
        if limite is not None:
            ids = ids[:max(0, limite)]
        for indice, tender_id in enumerate(ids):
            if parar and parar():
                resumo["pendentes"] = len(ids) - indice
                break
            tender = db.get(Tender, tender_id)
            if tender is None:
                continue
            try:
                asyncio.run(pncp_compras.sincronizar_itens(db, tender))
                db.refresh(tender)
            except Exception as exc:  # um edital com falha não interrompe a fila
                db.rollback()
                print(f"[AUTO-SYNC] Itens de {tender_id}: falha ({exc!r}).")
            if tender.itens_exclusivos_me_epp is None:
                _itens_sem_resultado[tender_id] = time.time()
                resumo["sem_itens"] += 1
            else:
                resumo["analisados"] += 1
            if estado.get("modo") == "noturna":
                estado["itens"] = dict(resumo, fila=len(ids))
            if pausa and indice < len(ids) - 1:
                time.sleep(pausa)
    finally:
        db.close()
    return resumo


# ------------------------------------------------------------------------------
# Rotinas
# ------------------------------------------------------------------------------
def _horario(texto: str) -> Tuple[int, int]:
    horas, _, minutos = texto.strip().partition(":")
    return int(horas), int(minutos or 0)


def janela_noturna(referencia: datetime) -> Tuple[datetime, datetime]:
    """Início e fim (Brasília, sem tzinfo) da janela noturna do dia de ``referencia``."""
    hi, mi = _horario(settings.AUTO_SYNC_NOITE_INICIO)
    hf, mf = _horario(settings.AUTO_SYNC_NOITE_FIM)
    base = referencia.replace(second=0, microsecond=0)
    return base.replace(hour=hi, minute=mi), base.replace(hour=hf, minute=mf)


def em_janela_noturna(agora: datetime) -> bool:
    inicio, fim = janela_noturna(agora)
    return inicio <= agora < fim


def _executar(modo: str, rotina: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Uma rotina por vez: a trava impede coleta diurna e noturna simultâneas."""
    if not _trava.acquire(blocking=False):
        return {"tipo": modo, "ignorado": "outra sincronização em andamento"}
    estado["modo"] = modo
    estado["em_execucao"] = modo
    try:
        resumo = rotina()
        estado["ultima_execucao"] = datetime.utcnow().isoformat()
        estado["ultimo_resultado"] = resumo
        return resumo
    finally:
        estado["modo"] = "repouso"
        estado["em_execucao"] = None
        estado["progresso"] = None
        estado.pop("itens", None)
        _trava.release()
        gc.collect()  # devolve na hora a memória dos lotes da coleta (Render: 512 MB)


def _rotina_diurna() -> Dict[str, Any]:
    inicio = datetime.utcnow()
    estado["ultima_diurna"] = agora_sao_paulo_naive()  # manual ou agendada: reinicia a contagem das 2 h
    resumo = _coletar_dias(_ultimos_dias(2), notificar_alertas=True, tipo="diurna")
    resumo["itens"] = enriquecer_itens(criados_desde=inicio, limite=settings.AUTO_SYNC_ITENS_DIURNO_MAX)
    # Pré-carregamento: itens dos certames que o operador deve abrir (sessão em 72 h, favoritos, AL, dispensas)
    from server.services.sync_items import pre_carregar_itens_estrategicos
    resumo["itens_estrategicos"] = pre_carregar_itens_estrategicos()
    return resumo


def executar_atualizacao() -> Dict[str, Any]:
    """Rotina diurna (e botão manual): últimas 24 h (hoje e ontem) + itens dos editais novos."""
    return _executar("diurna", _rotina_diurna)


def executar_noturno(fim_da_janela: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Consolidação noturna: relê os últimos AUTO_SYNC_DIAS dias inteiros (sem alertas do acervo) e,
    em paralelo, processa a fila de itens dos vigentes até ``fim_da_janela`` (Brasília).
    As duas filas usam APIs diferentes do PNCP (consulta e integração), cada uma no seu ritmo.
    """
    fim = fim_da_janela or janela_noturna(agora_sao_paulo_naive())[1]

    def _rotina() -> Dict[str, Any]:
        inicio = datetime.utcnow()
        resultado_itens: Dict[str, Any] = {}

        def _fila_de_itens() -> None:
            try:
                resultado_itens.update(enriquecer_itens(parar=lambda: agora_sao_paulo_naive() >= fim))
            except Exception as exc:
                resultado_itens["falha"] = repr(exc)

        fila = threading.Thread(target=_fila_de_itens, name="itens-noturnos", daemon=True)
        fila.start()
        resumo = _coletar_dias(_ultimos_dias(settings.AUTO_SYNC_DIAS), notificar_alertas=False,
                               tipo="noturna", consolidar_desde=inicio)
        fila.join()
        resumo["itens"] = resultado_itens
        return resumo

    return _executar("noturna", _rotina)


def catalogo_precisa_recuperacao() -> bool:
    """Catálogo ausente ou quase vazio (banco novo, recriado ou efêmero): o boot não pode ficar em repouso."""
    return contar_vigentes() < settings.AUTO_SYNC_MIN_VIGENTES


def executar_warmup(dias: Optional[int] = None) -> Dict[str, Any]:
    """Carga completa dos últimos ``dias``: recuperação de catálogo vazio ou uso administrativo."""
    return _executar("warmup", lambda: _coletar_dias(
        _ultimos_dias(dias or settings.AUTO_SYNC_DIAS), notificar_alertas=False, tipo="warmup"))


def disparar_em_background() -> bool:
    """
    Botão "Sincronizar agora": sempre e só a coleta das últimas 24 h, numa thread, sem bloquear a
    requisição. Devolve False se já houver uma sincronização em andamento.
    """
    if _trava.locked():
        return False

    def _rodar() -> None:
        try:
            print(f"[AUTO-SYNC] Sincronização manual (24 h) concluída: {executar_atualizacao()}")
        except Exception as exc:
            print(f"[AUTO-SYNC] Falha na sincronização manual: {exc!r}")

    threading.Thread(target=_rodar, name="sincronizacao-manual", daemon=True).start()
    return True


# ------------------------------------------------------------------------------
# Agendador
# ------------------------------------------------------------------------------
def ultima_coleta_registrada() -> Optional[datetime]:
    """Instante (UTC) da última página gravada; sobrevive a deploys, ao contrário do estado em memória."""
    db = SessionLocal()
    try:
        return db.query(func.max(SyncProgresso.atualizado_em)).scalar()
    finally:
        db.close()


def proxima_rotina(agora: datetime, ultima_diurna: Optional[datetime],
                   noite_concluida: Optional[date]) -> Tuple[str, datetime]:
    """
    Próxima rotina e quando roda (Brasília, sem tzinfo).
    - Dentro da janela noturna e sem consolidação feita nesta data: noturna, agora.
    - Senão, a diurna vence a cada intervalo desde a última; se cair na janela noturna, vai para o fim dela.
    - A noturna seguinte começa no próximo início de janela.
    """
    inicio_noite, fim_noite = janela_noturna(agora)
    if inicio_noite <= agora < fim_noite and noite_concluida != agora.date():
        return "noturna", agora

    intervalo = timedelta(minutes=max(5, settings.AUTO_SYNC_INTERVALO_MINUTOS))
    diurna = max((ultima_diurna + intervalo) if ultima_diurna else agora + intervalo, agora)
    i_noite, f_noite = janela_noturna(diurna)
    if i_noite <= diurna < f_noite:
        diurna = f_noite

    proxima_noite = inicio_noite if agora < inicio_noite else inicio_noite + timedelta(days=1)
    if proxima_noite.date() == noite_concluida:
        proxima_noite += timedelta(days=1)
    return ("noturna", proxima_noite) if proxima_noite <= diurna else ("diurna", diurna)


def _utc_para_brasilia(instante: Optional[datetime]) -> Optional[datetime]:
    if instante is None:
        return None
    return agora_sao_paulo_naive() - (datetime.utcnow() - instante)


def _brasilia_para_utc_iso(instante: datetime) -> str:
    return (datetime.utcnow() + (instante - agora_sao_paulo_naive())).isoformat()


async def loop_sincronizacao_automatica(atraso_inicial: float = 15.0,
                                        relogio: Callable[[], datetime] = agora_sao_paulo_naive) -> None:
    """
    Agendador híbrido. O arranque nunca coleta: o servidor fica em repouso e agenda a próxima rotina
    diurna (intervalo contado a partir da última coleta gravada no banco).
    """
    await asyncio.sleep(atraso_inicial)  # deixa o servidor terminar de subir
    ultima = await asyncio.to_thread(ultima_coleta_registrada)
    ultima_diurna = _utc_para_brasilia(ultima)
    if ultima and not estado.get("ultima_execucao"):
        estado["ultima_execucao"] = ultima.isoformat()
    arranque = relogio()
    nao_antes_de = arranque + timedelta(minutes=max(0, settings.AUTO_SYNC_REPOUSO_ARRANQUE_MINUTOS))
    # Deploy dentro da janela noturna: sem consolidação nesta noite (o boot nunca dispara a carga pesada)
    noite_concluida: Optional[date] = arranque.date() if em_janela_noturna(arranque) else None
    estado["modo"] = "repouso"
    if await asyncio.to_thread(catalogo_precisa_recuperacao):
        print(f"[AUTO-SYNC] Catálogo vazio ou incompleto no boot: recuperando os últimos {settings.AUTO_SYNC_DIAS} dias...")
        try:
            print(f"[AUTO-SYNC] Recuperação concluída: {await asyncio.to_thread(executar_warmup)}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[AUTO-SYNC] Falha na recuperação do catálogo: {exc!r}")
        ultima_diurna = relogio()
        nao_antes_de = relogio()
    while True:
        agora = relogio()
        manual = estado.get("ultima_diurna")
        if manual and (ultima_diurna is None or manual > ultima_diurna):
            ultima_diurna = manual
        tipo, quando = proxima_rotina(agora, ultima_diurna, noite_concluida)
        if tipo == "diurna" and quando < nao_antes_de:
            quando = nao_antes_de
        estado["proxima_rotina"] = tipo
        estado["proxima_execucao"] = _brasilia_para_utc_iso(quando)
        if quando > agora:
            # Dorme em fatias curtas: o relógio é reavaliado (mudança de dia, janela noturna)
            await asyncio.sleep(min(60.0, (quando - agora).total_seconds()))
            continue
        try:
            if tipo == "noturna":
                print(f"[AUTO-SYNC] Consolidação noturna: {settings.AUTO_SYNC_DIAS} dias + fila de itens...")
                resumo = await asyncio.to_thread(executar_noturno, janela_noturna(agora)[1])
            else:
                resumo = await asyncio.to_thread(executar_atualizacao)
            print(f"[AUTO-SYNC] Rotina {tipo} concluída: {resumo}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[AUTO-SYNC] Falha na rotina {tipo}: {exc!r}")
        if tipo == "noturna":
            noite_concluida = agora.date()
        else:
            ultima_diurna = max(relogio(), estado.get("ultima_diurna") or relogio())


ROTULO_DIURNA = "Sincronizando últimas 24h..."
ROTULO_NOTURNA = "Consolidação noturna (15 dias + itens) em andamento"


def _hhmm(utc_iso: Optional[str]) -> Optional[str]:
    if not utc_iso:
        return None
    try:
        return _utc_para_brasilia(datetime.fromisoformat(utc_iso)).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def situacao_para_exibicao() -> Dict[str, Any]:
    """Texto do indicador do cabeçalho (horários em Brasília)."""
    ultima, proxima = _hhmm(estado.get("ultima_execucao")), _hhmm(estado.get("proxima_execucao"))
    if estado.get("em_execucao") == "warmup":
        rotulo = f"Recuperando o catálogo (últimos {settings.AUTO_SYNC_DIAS} dias)..."
    elif estado.get("em_execucao") == "noturna":
        rotulo = ROTULO_NOTURNA.replace("15 dias", f"{settings.AUTO_SYNC_DIAS} dias")
    elif estado.get("em_execucao"):
        rotulo = ROTULO_DIURNA
    else:
        partes = [f"Atualizado às {ultima}" if ultima else None, f"Próxima verificação às {proxima}" if proxima else None]
        rotulo = " · ".join(p for p in partes if p) or "Atualização automática ativa"
    return {
        "rotulo": rotulo,
        "ultima_execucao_hhmm": ultima,
        "proxima_execucao_hhmm": proxima,
        "janela_noturna": f"{settings.AUTO_SYNC_NOITE_INICIO}-{settings.AUTO_SYNC_NOITE_FIM}",
    }
