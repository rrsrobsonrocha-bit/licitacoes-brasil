"""
Abastecimento contínuo do catálogo global de editais (Licitações Brasil).

O catálogo é compartilhado por todas as contas e permanente: esta rotina só insere ou atualiza
certames devolvidos pela API oficial do PNCP (upsert por numeroControlePNCP) e nunca apaga nada.
Sem resposta da API, nada é gravado.

- Auto-warmup: percorre os últimos ``AUTO_SYNC_DIAS`` dias de publicação, do mais recente para o
  mais antigo, um dia por vez; cada dia e modalidade é paginado até a última página do PNCP e cada
  página é gravada assim que chega. O ritmo respeita o limite da API (ver connectors/pncp.py), então
  o warmup completo leva da ordem de uma hora; o andamento fica em ``estado["progresso"]``.
- Retomada: o progresso de cada dia/modalidade fica na tabela ``sync_progresso``. Após um novo
  deploy, dias encerrados já concluídos são pulados e um dia interrompido continua da página
  seguinte. Hoje e ontem ainda recebem publicações: retomam da última página lida (relida para
  pegar os acréscimos), sem recomeçar do zero. Duplicidade é impossível (upsert por numeroControlePNCP).
- Atualização periódica: hoje e ontem, com entrega dos alertas dos usuários para os editais novos.
"""
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from server.config import settings
from server.database import SessionLocal
from server.integrations.opportunity_collector import sync_procurement_opportunities
from server.models import SyncProgresso, Tender, hoje_sao_paulo
from server.search_engine import filtro_vigentes

# Modalidades com disputa: 6 = Pregão Eletrônico, 8 = Dispensa de Licitação
MODALIDADES_DISPUTA = [6, 8]

_trava = threading.Lock()
estado: Dict[str, Any] = {
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


def plano_de_retomada(dia: str) -> Dict[int, int]:
    """
    Página inicial de cada modalidade do dia; modalidades ausentes do resultado já foram concluídas.
    Dias encerrados (anteriores a ontem) não mudam mais no PNCP: concluídos são pulados e os
    interrompidos seguem da próxima página. Hoje e ontem relêem a última página lida.
    """
    salvos = _progresso_salvo(dia)
    encerrado = dia < (hoje_sao_paulo() - timedelta(days=1)).strftime("%Y%m%d")
    plano: Dict[int, int] = {}
    for codigo in MODALIDADES_DISPUTA:
        registro = salvos.get(codigo)
        if not registro:
            plano[codigo] = 1
        elif encerrado:
            if not registro.concluido:
                plano[codigo] = registro.ultima_pagina + 1
        else:
            plano[codigo] = max(1, registro.ultima_pagina)
    return plano


def _coletar_dias(dias: List[str], notificar_alertas: bool, tipo: str) -> Dict[str, Any]:
    """Coleta cada dia (YYYYMMDD) em sessão própria. Um dia com falha não interrompe os demais."""
    if not _trava.acquire(blocking=False):
        return {"tipo": tipo, "ignorado": "outra sincronização automática em andamento"}
    inicio = datetime.utcnow()
    estado["em_execucao"] = tipo
    resumo = {"tipo": tipo, "dias": len(dias), "dias_ja_concluidos": 0, "coletados": 0, "novos": 0,
              "atualizados": 0, "falhas": []}
    try:
        for indice, dia in enumerate(dias, start=1):
            plano = plano_de_retomada(dia)
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
        estado["ultima_execucao"] = datetime.utcnow().isoformat()
        estado["ultimo_resultado"] = resumo
        return resumo
    finally:
        estado["em_execucao"] = None
        estado["progresso"] = None
        _trava.release()


def _ultimos_dias(quantidade: int) -> List[str]:
    hoje = hoje_sao_paulo()
    return [(hoje - timedelta(days=i)).strftime("%Y%m%d") for i in range(max(1, quantidade))]


def executar_warmup(dias: Optional[int] = None) -> Dict[str, Any]:
    """Coleta os últimos ``dias`` (padrão AUTO_SYNC_DIAS), sem disparar alertas do acervo retroativo."""
    return _coletar_dias(_ultimos_dias(dias or settings.AUTO_SYNC_DIAS), notificar_alertas=False, tipo="warmup")


def executar_atualizacao() -> Dict[str, Any]:
    """Publicações de hoje e de ontem (Brasília), com entrega de alertas para os editais novos."""
    return _coletar_dias(_ultimos_dias(2), notificar_alertas=True, tipo="atualizacao")


def disparar_em_background() -> bool:
    """
    Sincronização sob demanda (botão "Sincronizar agora"): roda numa thread, sem bloquear a
    requisição. Devolve False se já houver uma sincronização em andamento.
    """
    if _trava.locked():
        return False

    def _rodar() -> None:
        try:
            resumo = executar_warmup() if precisa_warmup(no_arranque=False) else executar_atualizacao()
            print(f"[AUTO-SYNC] Sincronização manual concluída: {resumo}")
        except Exception as exc:
            print(f"[AUTO-SYNC] Falha na sincronização manual: {exc!r}")

    threading.Thread(target=_rodar, name="sincronizacao-manual", daemon=True).start()
    return True


def precisa_warmup(no_arranque: bool) -> bool:
    return (no_arranque and em_producao()) or contar_vigentes() < settings.AUTO_SYNC_MIN_VIGENTES


async def loop_sincronizacao_automatica(atraso_inicial: float = 15.0) -> None:
    """Warmup no arranque e atualização periódica do catálogo, sem depender de nenhum usuário."""
    await asyncio.sleep(atraso_inicial)  # deixa o servidor terminar de subir
    no_arranque = True
    while True:
        try:
            if await asyncio.to_thread(precisa_warmup, no_arranque):
                print(f"[AUTO-SYNC] Warmup: coletando os últimos {settings.AUTO_SYNC_DIAS} dias do PNCP...")
                resumo = await asyncio.to_thread(executar_warmup)
            else:
                resumo = await asyncio.to_thread(executar_atualizacao)
            print(f"[AUTO-SYNC] Concluído: {resumo}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[AUTO-SYNC] Falha na sincronização automática: {exc!r}")
        no_arranque = False
        intervalo = max(5, settings.AUTO_SYNC_INTERVALO_MINUTOS) * 60
        estado["proxima_execucao"] = (datetime.utcnow() + timedelta(seconds=intervalo)).isoformat()
        await asyncio.sleep(intervalo)
