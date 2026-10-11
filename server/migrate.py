"""
Migrações idempotentes do banco (SQLite em desenvolvimento/Render; compatível com PostgreSQL).

Regras:
  - Cada instrução DDL roda em transação própria, para que uma falha "já existe"
    não aborte as demais (no PostgreSQL um erro invalida a transação inteira).
  - Colunas só são adicionadas se ausentes; erros de "duplicate column" são ignorados.
  - Índices usam CREATE INDEX IF NOT EXISTS e são deduplicados por nome.
  - Tabelas auxiliares são criadas a partir dos modelos com checkfirst=True.
  - Pode ser executado quantas vezes for necessário sem efeitos colaterais.
"""
import logging
from typing import Iterable, List, Set, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from server.database import engine

logger = logging.getLogger(__name__)

_MARCADORES_JA_EXISTE = (
    "already exists",
    "duplicate column",
    "duplicate key name",
    "duplicate_object",
    "já existe",
)

# (tabela, coluna, tipo/DDL da coluna)
COLUNAS_SAVED_SEARCHES: List[Tuple[str, str, str]] = [
    ("saved_searches", "ufs", "VARCHAR(255)"),
    ("saved_searches", "min_value", "FLOAT"),
]

COLUNAS_TENDERS: List[Tuple[str, str, str]] = [
    ("tenders", "source_system", "VARCHAR(50) DEFAULT 'pncp' NOT NULL"),
    ("tenders", "uasg_code", "VARCHAR(50)"),
    ("tenders", "sicaf_ready", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("tenders", "link_comprasnet", "VARCHAR(1000)"),
    ("tenders", "link_pncp", "VARCHAR(1000)"),
    ("tenders", "url_disputa", "VARCHAR(1000)"),
    ("tenders", "url_pncp", "VARCHAR(1000)"),
    ("tenders", "fonte_portal", "VARCHAR(100) DEFAULT 'PNCP'"),
    ("tenders", "link_edital_oficial", "VARCHAR(1000)"),
    ("tenders", "ai_resumo", "TEXT"),
    ("tenders", "ai_pontos_atencao", "TEXT"),
    ("tenders", "itens_extraidos_json", "TEXT"),
    ("tenders", "natureza_objeto", "VARCHAR(20)"),
    ("tenders", "itens_exclusivos_me_epp", "INTEGER"),
    ("tenders", "itens_ampla_concorrencia", "INTEGER"),
]

COLUNAS_ITENS_DOCUMENTOS: List[Tuple[str, str, str]] = [
    ("tender_items", "material_ou_servico", "VARCHAR(1)"),
    ("tender_items", "criterio_julgamento", "VARCHAR(100)"),
    ("tender_items", "situacao", "VARCHAR(100)"),
    ("tender_items", "beneficio", "VARCHAR(150)"),
    ("tender_items", "orcamento_sigiloso", "BOOLEAN"),
    ("tender_items", "informacao_complementar", "TEXT"),
    ("tender_items", "origem", "VARCHAR(20)"),
    ("tender_items", "synced_at", "DATETIME"),
    ("tender_documents", "sequencial_documento", "INTEGER"),
    ("tender_documents", "origem", "VARCHAR(20)"),
]

# Assinatura: contas existentes ficam no plano gratuito, ativas e sem data de expiração
COLUNAS_USERS: List[Tuple[str, str, str]] = [
    ("users", "plan_type", "VARCHAR(20) DEFAULT 'GRATUITO' NOT NULL"),
    ("users", "subscription_status", "VARCHAR(20) DEFAULT 'ACTIVE' NOT NULL"),
    ("users", "access_expires_at", "TIMESTAMP"),
    ("users", "hotmart_subscriber_code", "VARCHAR(100)"),
]

COLUNAS_DEALS: List[Tuple[str, str, str]] = [
    ("deals", "selected_items", "JSON"),
    ("deals", "user_id", "VARCHAR(36)"),
]

# Anotação gravada pelo antigo seed de demonstração do funil (removido): identifica os negócios fictícios.
NOTA_DEAL_SEED_DEMO = "Certame prioritário acompanhado no estágio %"

# (nome do índice, tabela, colunas)
INDICES_EXPLICITOS: List[Tuple[str, str, str]] = [
    ("ix_tenders_opening_date", "tenders", "opening_date"),
    ("ix_tenders_status_opening", "tenders", "status, opening_date"),
    ("ix_tenders_url_pncp", "tenders", "url_pncp"),
    ("ix_tenders_url_disputa", "tenders", "url_disputa"),
    ("ix_tenders_natureza_objeto", "tenders", "natureza_objeto"),
    # Editais vigentes: prazo de propostas futuro, combinado com status, UF e modalidade
    ("ix_tenders_deadline_status", "tenders", "proposal_deadline, status"),
    ("ix_tenders_state_deadline", "tenders", "state, proposal_deadline"),
    ("ix_tenders_modality_deadline", "tenders", "modality, proposal_deadline"),
    # Filtros avançados da busca: benefício ME/EPP, esfera e portal de disputa
    ("ix_tenders_itens_exclusivos_me_epp", "tenders", "itens_exclusivos_me_epp"),
    ("ix_tenders_itens_ampla_concorrencia", "tenders", "itens_ampla_concorrencia"),
    ("ix_tenders_sphere_deadline", "tenders", "administrative_sphere, proposal_deadline"),
]


# ------------------------------------------------------------------------------
# Utilitários idempotentes
# ------------------------------------------------------------------------------
def _ja_existe(exc: Exception) -> bool:
    mensagem = str(getattr(exc, "orig", exc)).lower()
    return any(marcador in mensagem for marcador in _MARCADORES_JA_EXISTE)


def _executar_ddl(sql: str, descricao: str) -> bool:
    """Executa uma instrução DDL isolada. Retorna False se a estrutura já existia."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        return True
    except (OperationalError, ProgrammingError) as exc:
        if _ja_existe(exc):
            logger.debug("Migração: %s já existe, ignorado.", descricao)
            return False
        raise


def _tabelas_existentes() -> Set[str]:
    return set(inspect(engine).get_table_names())


def _colunas_existentes(tabela: str) -> Set[str]:
    return {c["name"] for c in inspect(engine).get_columns(tabela)}


def _adicionar_colunas(colunas: Iterable[Tuple[str, str, str]]) -> None:
    tabelas = _tabelas_existentes()
    cache: dict = {}
    for tabela, coluna, ddl in colunas:
        if tabela not in tabelas:
            continue
        if tabela not in cache:
            cache[tabela] = _colunas_existentes(tabela)
        if coluna in cache[tabela]:
            continue
        logger.info("Migração: adicionando coluna %s à tabela %s", coluna, tabela)
        if _executar_ddl(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}", f"coluna {tabela}.{coluna}"):
            cache[tabela].add(coluna)


def _criar_indice(nome: str, tabela: str, colunas: str) -> None:
    _executar_ddl(f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})", f"índice {nome}")


def _criar_tabela_do_modelo(modelo) -> None:
    """Cria a tabela (e seus índices) a partir do modelo, somente se ainda não existir."""
    tabela = modelo.__table__
    if tabela.name in _tabelas_existentes():
        return
    logger.info("Migração: criando tabela %s", tabela.name)
    try:
        tabela.create(bind=engine, checkfirst=True)
    except (OperationalError, ProgrammingError) as exc:
        if not _ja_existe(exc):
            raise
        logger.debug("Migração: estrutura de %s já existia (%s).", tabela.name, exc)


def _garantir_indices_dos_modelos() -> None:
    """Cria, se faltarem, os índices declarados nos modelos (deduplicados por nome)."""
    from server.database import Base
    import server.models  # noqa: F401  (registra os modelos no metadata)

    tabelas = _tabelas_existentes()
    vistos: Set[str] = set()
    for tabela in Base.metadata.sorted_tables:
        if tabela.name not in tabelas:
            continue
        colunas = _colunas_existentes(tabela.name)
        for indice in sorted(tabela.indexes, key=lambda i: i.name or ""):
            # Índices únicos não são criados aqui: em bases legadas com dados duplicados
            # falhariam com IntegrityError e derrubariam o deploy.
            if not indice.name or indice.name in vistos or indice.unique:
                continue
            vistos.add(indice.name)
            nomes_colunas = [c.name for c in indice.columns]
            if not nomes_colunas or any(n not in colunas for n in nomes_colunas):
                continue
            _executar_ddl(
                f"CREATE INDEX IF NOT EXISTS {indice.name} ON {tabela.name} ({', '.join(nomes_colunas)})",
                f"índice {indice.name}",
            )


# ------------------------------------------------------------------------------
# Etapas
# ------------------------------------------------------------------------------
def _backfill_identificadores() -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE tenders SET link_pncp = source_url WHERE link_pncp IS NULL"))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'compras_gov', sicaf_ready = 1, uasg_code = '250005', "
            "link_comprasnet = 'https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=250005' "
            "WHERE administrative_sphere = 'FEDERAL' AND (source_system IS NULL OR source_system = 'pncp')"
        ))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'pncp', sicaf_ready = 0 "
            "WHERE administrative_sphere != 'FEDERAL' AND source_system IS NULL"
        ))


def _backfill_natureza_objeto(lote: int = 500) -> None:
    """Classifica (MATERIAL / SERVICO / OUTROS) os certames ainda sem natureza. Idempotente."""
    from server.services.portal_collector import classificar_natureza_certame

    total = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, title, object_description FROM tenders WHERE natureza_objeto IS NULL LIMIT :lote"),
                {"lote": lote},
            ).fetchall()
            if not rows:
                break
            for t_id, titulo, descricao in rows:
                conn.execute(
                    text("UPDATE tenders SET natureza_objeto = :nat WHERE id = :t_id"),
                    {"nat": classificar_natureza_certame(titulo, descricao), "t_id": t_id},
                )
            total += len(rows)
        if len(rows) < lote:
            break
    if total:
        logger.info("Migração: natureza do objeto classificada em %s licitações.", total)


def _remover_deals_de_demonstracao() -> None:
    """Apaga os negócios fictícios criados pelo antigo seed do funil (sem dono). Idempotente."""
    if "user_id" not in _colunas_existentes("deals"):
        return
    with engine.begin() as conn:
        removidos = conn.execute(
            text("DELETE FROM deals WHERE user_id IS NULL AND notes LIKE :nota"),
            {"nota": NOTA_DEAL_SEED_DEMO},
        ).rowcount
    if removidos:
        logger.info("Migração: %s negócios de demonstração removidos do funil.", removidos)


def _remover_contas_demonstracao() -> None:
    """
    Apaga em definitivo as antigas contas padrão de demonstração (ativas ou já desativadas).
    Antes, transfere para a conta principal (ORPHAN_OWNER_EMAIL, depois ADMIN_EMAIL; ver
    _dono_dos_registros_orfaos) tudo o que foi criado nelas: negócios, buscas salvas, alertas,
    histórico, favoritos e anotações. Para nunca perder esses dados, só age quando já existe uma
    conta real ativa. Idempotente.
    """
    from server.database import SessionLocal
    from server.models import (
        Alert, Deal, Favorite, PasswordResetToken, SavedSearch, SearchHistory, User, UserNote,
    )
    from server.seed_data import EMAILS_CONTAS_DEMONSTRACAO

    db = SessionLocal()
    try:
        demos = db.query(User).filter(User.email.in_(EMAILS_CONTAS_DEMONSTRACAO)).all()
        if not demos:
            return
        principal = _dono_dos_registros_orfaos(db)
        if not principal:
            logger.warning(
                "Migração: contas de demonstração mantidas porque ainda não há conta real para receber "
                "os dados delas. Defina ADMIN_EMAIL/ADMIN_PASSWORD (ou cadastre uma conta) e reinicie."
            )
            return
        for demo in demos:
            for modelo in (Deal, SavedSearch, Alert, SearchHistory):
                db.query(modelo).filter(modelo.user_id == demo.id).update(
                    {modelo.user_id: principal.id}, synchronize_session=False
                )
            # Favoritos e anotações são únicos por usuário + certame: duplicados ficam com a principal
            for modelo in (Favorite, UserNote):
                ja_tem = {r.tender_id for r in db.query(modelo.tender_id).filter(modelo.user_id == principal.id)}
                for registro in db.query(modelo).filter(modelo.user_id == demo.id).all():
                    if registro.tender_id in ja_tem:
                        db.delete(registro)
                    else:
                        registro.user_id = principal.id
            db.query(PasswordResetToken).filter(PasswordResetToken.user_id == demo.id).delete(synchronize_session=False)
            db.flush()
            db.expire(demo)
            db.delete(demo)
            logger.warning("Migração: conta de demonstração %s apagada; dados transferidos para %s.",
                           demo.email, principal.email)
        db.commit()
    finally:
        db.close()


def _remover_certames_de_exemplo() -> None:
    """
    Remove do catálogo os certames fictícios do antigo seed (com itens, documentos e análises).
    Um certame de exemplo que algum usuário tenha posto no funil, favoritado ou anotado é mantido.
    """
    from server.database import SessionLocal
    from server.models import Deal, Favorite, Tender, UserNote
    from server.seed_data import IDS_CERTAMES_EXEMPLO

    db = SessionLocal()
    try:
        removidos = 0
        for tender in db.query(Tender).filter(Tender.id.in_(IDS_CERTAMES_EXEMPLO)).all():
            em_uso = (
                db.query(Deal).filter(Deal.tender_id == tender.id).first()  # inclui negócios legados (demos já saíram)
                or db.query(Favorite).filter(Favorite.tender_id == tender.id).first()
                or db.query(UserNote).filter(UserNote.tender_id == tender.id).first()
            )
            if em_uso:
                logger.info("Migração: certame de exemplo %s mantido (em uso por um usuário).", tender.id)
                continue
            db.delete(tender)
            removidos += 1
        db.commit()
        if removidos:
            logger.info("Migração: %s certames de exemplo removidos do catálogo.", removidos)
    finally:
        db.close()


def _ids_certames_ficticios_legados() -> List[str]:
    import json
    from pathlib import Path

    arquivo = Path(__file__).resolve().parent / "data" / "certames_ficticios_legados.json"
    try:
        return list(json.loads(arquivo.read_text(encoding="utf-8")).get("external_ids", []))
    except (OSError, ValueError):
        logger.warning("Migração: lista de certames fictícios legados indisponível (%s).", arquivo)
        return []


def _remover_certames_ficticios_legados(lote: int = 200) -> None:
    """
    Remove do catálogo global os certames inventados que versões antigas do coletor gravavam
    (catálogo de "fallback"/contingência), identificados pelo external_id. Certames em uso por
    algum usuário (funil, favorito, anotação) são mantidos. Idempotente.
    """
    from server.database import SessionLocal
    from server.models import Deal, Favorite, Tender, UserNote

    ids = _ids_certames_ficticios_legados()
    if not ids:
        return
    db = SessionLocal()
    try:
        removidos = mantidos = 0
        for inicio in range(0, len(ids), lote):
            for tender in db.query(Tender).filter(Tender.external_id.in_(ids[inicio:inicio + lote])).all():
                em_uso = (
                    db.query(Deal.id).filter(Deal.tender_id == tender.id).first()
                    or db.query(Favorite.id).filter(Favorite.tender_id == tender.id).first()
                    or db.query(UserNote.id).filter(UserNote.tender_id == tender.id).first()
                )
                if em_uso:
                    mantidos += 1
                    continue
                db.delete(tender)
                removidos += 1
            db.commit()
        if removidos or mantidos:
            logger.info("Migração: %s certames fictícios legados removidos (%s mantidos por estarem em uso).",
                        removidos, mantidos)
    finally:
        db.close()


DOMINIO_HUB_MULTIFONTE = "licitacoesbrasil.gov.br"


def _migrar_hub_multifonte_para_pncp() -> None:
    """
    Remove a antiga fonte genérica "Hub Multifonte" (domínio fictício licitacoesbrasil.gov.br),
    criada pelo coletor multifonte para agrupar certames de várias origens. Os certames ligados
    a ela vêm da API pública do PNCP: primeiro são reatribuídos à fonte oficial pncp-source-01 e
    só depois a fonte é apagada, porque tenders.source_id usa ON DELETE CASCADE. Idempotente.
    """
    from server.database import SessionLocal
    from server.models import Source, Tender
    from server.seed_data import garantir_fonte_pncp

    db = SessionLocal()
    try:
        hubs = [s.id for s in db.query(Source.id).filter(Source.official_domain == DOMINIO_HUB_MULTIFONTE)]
        if not hubs:
            return
        pncp = garantir_fonte_pncp(db)
        movidos = db.query(Tender).filter(Tender.source_id.in_(hubs)).update(
            {Tender.source_id: pncp.id}, synchronize_session=False
        )
        db.commit()
        restantes = db.query(Tender.id).filter(Tender.source_id.in_(hubs)).count()
        if restantes:
            logger.warning("Migração: fonte Hub Multifonte mantida (%s certames ainda ligados a ela).", restantes)
            return
        db.query(Source).filter(Source.id.in_(hubs)).delete(synchronize_session=False)
        db.commit()
        logger.info("Migração: %s certames reatribuídos ao PNCP oficial e fonte Hub Multifonte removida.", movidos)
    finally:
        db.close()


def _garantir_admin_inicial() -> None:
    from server.database import SessionLocal
    from server.seed_data import garantir_admin_inicial

    db = SessionLocal()
    try:
        garantir_admin_inicial(db)
    finally:
        db.close()


def _dono_dos_registros_orfaos(db):
    """
    Conta que recebe os registros legados sem dono: ORPHAN_OWNER_EMAIL, depois ADMIN_EMAIL;
    senão a conta real principal (administrador mais recente a entrar, depois o usuário mais
    recente a entrar, depois o mais antigo). Contas de demonstração nunca são escolhidas.
    """
    from server.config import settings
    from server.models import User
    from server.seed_data import EMAILS_CONTAS_DEMONSTRACAO

    reais = db.query(User).filter(User.status == "ACTIVE", ~User.email.in_(EMAILS_CONTAS_DEMONSTRACAO))
    for email in (settings.ORPHAN_OWNER_EMAIL, settings.ADMIN_EMAIL):
        email = (email or "").strip().lower()
        if email:
            user = reais.filter(User.email == email).first()
            if user:
                return user
    return (
        reais.filter(User.role == "ADMIN", User.last_login_at.isnot(None)).order_by(User.last_login_at.desc()).first()
        or reais.filter(User.last_login_at.isnot(None)).order_by(User.last_login_at.desc()).first()
        or reais.order_by(User.created_at.asc()).first()
    )


def _associar_registros_orfaos() -> None:
    """
    Atribui à conta principal os negócios e alertas legados sem dono (user_id nulo), para que
    nenhum dado real cadastrado antes do isolamento por usuário fique invisível. Idempotente:
    sem órfãos ou sem conta real cadastrada, não faz nada (tenta de novo no próximo boot).
    """
    from server.database import SessionLocal
    from server.models import Alert, Deal, SavedSearch

    db = SessionLocal()
    try:
        modelos = (Deal, SavedSearch, Alert)
        if not any(db.query(m).filter(m.user_id.is_(None)).first() for m in modelos):
            return
        dono = _dono_dos_registros_orfaos(db)
        if not dono:
            logger.warning("Migração: há registros sem dono, mas nenhuma conta real para recebê-los ainda.")
            return
        for modelo in modelos:
            total = db.query(modelo).filter(modelo.user_id.is_(None)).update(
                {modelo.user_id: dono.id}, synchronize_session=False
            )
            if total:
                logger.info("Migração: %s registros de %s atribuídos a %s.", total, modelo.__tablename__, dono.email)
        db.commit()
    finally:
        db.close()


def _backfill_links_canonicos() -> None:
    """Backfill canônico de URLs do PNCP e links de disputa (idempotente: recalcula os mesmos valores)."""
    from server.connectors.pncp import build_canonical_pncp_url

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, external_id, source_url, link_pncp, link_comprasnet, official_notice_url, url_disputa, url_pncp FROM tenders"
        )).fetchall()

        updated_count = 0
        for r in rows:
            t_id, ext_id, src_url, l_pncp, l_cnet, off_url, u_disp, u_pncp = r

            canon_pncp = build_canonical_pncp_url(numero_controle_pncp=ext_id or l_pncp or src_url)

            if l_cnet:
                dispute = l_cnet
            elif off_url and "pncp.gov.br" not in off_url:
                dispute = off_url
            elif u_disp and "pncp.gov.br" not in u_disp:
                dispute = u_disp
            else:
                dispute = canon_pncp

            if (u_pncp, l_pncp, src_url, u_disp) == (canon_pncp, canon_pncp, canon_pncp, dispute):
                continue

            conn.execute(
                text("""
                    UPDATE tenders
                    SET url_pncp = :canon_pncp,
                        link_pncp = :canon_pncp,
                        source_url = :canon_pncp,
                        url_disputa = :dispute
                    WHERE id = :t_id
                """),
                {"canon_pncp": canon_pncp, "dispute": dispute, "t_id": t_id},
            )
            updated_count += 1

        logger.info("Migração: %s licitações atualizadas com links canônicos do PNCP e disputa.", updated_count)


def run_db_migrations():
    """
    Executa migrações automáticas no banco SQLite/PostgreSQL garantindo que todas as
    colunas, tabelas e índices existam. Seguro para execução repetida (idempotente).
    """
    if "tenders" not in _tabelas_existentes():
        # Banco ainda sem esquema: Base.metadata.create_all (server/main.py) cria tudo.
        return

    from server.models import DocumentoEmpresa, HistoricoDisputa, PasswordResetToken, SyncProgresso

    # 1. Colunas novas em tabelas existentes
    _adicionar_colunas(COLUNAS_SAVED_SEARCHES)
    _adicionar_colunas(COLUNAS_TENDERS)
    _adicionar_colunas(COLUNAS_ITENS_DOCUMENTOS)
    _adicionar_colunas(COLUNAS_DEALS)
    _adicionar_colunas(COLUNAS_USERS)

    # 1.1 Dados de demonstração antigos e registros órfãos (ordem importa: demos saem antes da atribuição)
    if "deals" in _tabelas_existentes():
        _remover_deals_de_demonstracao()
    from server.config import settings
    if "sources" in _tabelas_existentes():
        _migrar_hub_multifonte_para_pncp()
    if "users" in _tabelas_existentes():
        # Conta ADMIN_EMAIL criada/atualizada já aqui, para receber os dados legados no mesmo boot
        _garantir_admin_inicial()
        if settings.REMOVER_DADOS_DEMONSTRACAO:
            _remover_contas_demonstracao()
            _remover_certames_de_exemplo()
            _remover_certames_ficticios_legados()
        if "deals" in _tabelas_existentes():
            _associar_registros_orfaos()

    # 2. Tabelas auxiliares (auditoria de disputas, cofre de documentos e recuperação de senha)
    _criar_tabela_do_modelo(HistoricoDisputa)
    _criar_tabela_do_modelo(DocumentoEmpresa)
    _criar_tabela_do_modelo(SyncProgresso)
    if "users" in _tabelas_existentes():
        _criar_tabela_do_modelo(PasswordResetToken)

    # 3. Backfill de links, identificadores e natureza do objeto para dados existentes
    _backfill_identificadores()
    _backfill_natureza_objeto()

    # 4. Índices de alta performance (explícitos + declarados nos modelos)
    for nome, tabela, colunas in INDICES_EXPLICITOS:
        _criar_indice(nome, tabela, colunas)
    _garantir_indices_dos_modelos()

    # 5. Backfill canônico de URLs do PNCP e links de disputa
    _backfill_links_canonicos()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_db_migrations()
    print("Migrações executadas com sucesso.")
