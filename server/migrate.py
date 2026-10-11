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

COLUNAS_DEALS: List[Tuple[str, str, str]] = [
    ("deals", "selected_items", "JSON"),
]

# (nome do índice, tabela, colunas)
INDICES_EXPLICITOS: List[Tuple[str, str, str]] = [
    ("ix_tenders_opening_date", "tenders", "opening_date"),
    ("ix_tenders_status_opening", "tenders", "status, opening_date"),
    ("ix_tenders_url_pncp", "tenders", "url_pncp"),
    ("ix_tenders_url_disputa", "tenders", "url_disputa"),
    ("ix_tenders_natureza_objeto", "tenders", "natureza_objeto"),
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

    from server.models import DocumentoEmpresa, HistoricoDisputa, PasswordResetToken

    # 1. Colunas novas em tabelas existentes
    _adicionar_colunas(COLUNAS_SAVED_SEARCHES)
    _adicionar_colunas(COLUNAS_TENDERS)
    _adicionar_colunas(COLUNAS_ITENS_DOCUMENTOS)
    _adicionar_colunas(COLUNAS_DEALS)

    # 2. Tabelas auxiliares (auditoria de disputas, cofre de documentos e recuperação de senha)
    _criar_tabela_do_modelo(HistoricoDisputa)
    _criar_tabela_do_modelo(DocumentoEmpresa)
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
