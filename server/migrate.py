import logging
from sqlalchemy import text, inspect
from server.database import engine

logger = logging.getLogger(__name__)

def run_db_migrations():
    """
    Executa migrações automáticas no banco SQLite/PostgreSQL
    garantindo que todas as novas colunas e índices existam.
    """
    inspector = inspect(engine)
    if "tenders" not in inspector.get_table_names():
        return

    if "saved_searches" in inspector.get_table_names():
        s_cols = [c["name"] for c in inspector.get_columns("saved_searches")]
        with engine.begin() as conn:
            if "ufs" not in s_cols:
                logger.info("Migração: adicionando coluna ufs à tabela saved_searches")
                conn.execute(text("ALTER TABLE saved_searches ADD COLUMN ufs VARCHAR(255)"))
            if "min_value" not in s_cols:
                logger.info("Migração: adicionando coluna min_value à tabela saved_searches")
                conn.execute(text("ALTER TABLE saved_searches ADD COLUMN min_value FLOAT"))

    cols = [c["name"] for c in inspector.get_columns("tenders")]
    with engine.begin() as conn:
        if "source_system" not in cols:
            logger.info("Migração: adicionando coluna source_system à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN source_system VARCHAR(50) DEFAULT 'pncp' NOT NULL"))
        if "uasg_code" not in cols:
            logger.info("Migração: adicionando coluna uasg_code à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN uasg_code VARCHAR(50)"))
        if "sicaf_ready" not in cols:
            logger.info("Migração: adicionando coluna sicaf_ready à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN sicaf_ready BOOLEAN DEFAULT 0 NOT NULL"))
        if "link_comprasnet" not in cols:
            logger.info("Migração: adicionando coluna link_comprasnet à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_comprasnet VARCHAR(1000)"))
        if "link_pncp" not in cols:
            logger.info("Migração: adicionando coluna link_pncp à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_pncp VARCHAR(1000)"))
        if "url_disputa" not in cols:
            logger.info("Migração: adicionando coluna url_disputa à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN url_disputa VARCHAR(1000)"))
        if "url_pncp" not in cols:
            logger.info("Migração: adicionando coluna url_pncp à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN url_pncp VARCHAR(1000)"))
        if "fonte_portal" not in cols:
            logger.info("Migração: adicionando coluna fonte_portal à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN fonte_portal VARCHAR(100) DEFAULT 'PNCP'"))
        if "link_edital_oficial" not in cols:
            logger.info("Migração: adicionando coluna link_edital_oficial à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_edital_oficial VARCHAR(1000)"))
        if "ai_resumo" not in cols:
            logger.info("Migração: adicionando coluna ai_resumo à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN ai_resumo TEXT"))
        if "ai_pontos_atencao" not in cols:
            logger.info("Migração: adicionando coluna ai_pontos_atencao à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN ai_pontos_atencao TEXT"))
        if "itens_extraidos_json" not in cols:
            logger.info("Migração: adicionando coluna itens_extraidos_json à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN itens_extraidos_json TEXT"))

        # Criação da tabela de auditoria de disputas se inexistente
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS historico_disputas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pregao VARCHAR(255) NOT NULL,
                numero_item INTEGER NOT NULL,
                tipo_evento VARCHAR(50) NOT NULL,
                valor_ofertado FLOAT NOT NULL,
                posicao INTEGER DEFAULT 1,
                autor VARCHAR(100) DEFAULT 'Robô LicitaçõesBrasil',
                detalhes TEXT,
                created_at DATETIME NOT NULL
            )
        """))

        # Cofre de documentos de habilitação da empresa (DDL portátil via metadata do modelo)
        if "documentos_empresa" not in inspector.get_table_names():
            from server.models import DocumentoEmpresa
            logger.info("Migração: criando tabela documentos_empresa")
            DocumentoEmpresa.__table__.create(bind=conn, checkfirst=True)

        # Backfill de links e identificadores para dados existentes
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

        # Garantir índices de alta performance
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_opening_date ON tenders (opening_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_status_opening ON tenders (status, opening_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_url_pncp ON tenders (url_pncp)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_url_disputa ON tenders (url_disputa)"))
    # Backfill canônico de URLs do PNCP e links de disputa
    from server.connectors.pncp import build_canonical_pncp_url
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, external_id, source_url, link_pncp, link_comprasnet, official_notice_url, url_disputa, url_pncp FROM tenders"
        )).fetchall()

        updated_count = 0
        for r in rows:
            t_id, ext_id, src_url, l_pncp, l_cnet, off_url, u_disp, u_pncp = r

            # Gera link canônico do PNCP
            canon_pncp = build_canonical_pncp_url(
                numero_controle_pncp=ext_id or l_pncp or src_url
            )

            # Prioriza link de disputa / portal de origem
            dispute = None
            if l_cnet:
                dispute = l_cnet
            elif off_url and "pncp.gov.br" not in off_url:
                dispute = off_url
            elif u_disp and "pncp.gov.br" not in u_disp:
                dispute = u_disp
            else:
                dispute = canon_pncp

            conn.execute(
                text("""
                    UPDATE tenders 
                    SET url_pncp = :canon_pncp,
                        link_pncp = :canon_pncp,
                        source_url = :canon_pncp,
                        url_disputa = :dispute
                    WHERE id = :t_id
                """),
                {"canon_pncp": canon_pncp, "dispute": dispute, "t_id": t_id}
            )
            updated_count += 1

        logger.info(f"Migração: {updated_count} licitações atualizadas com links canônicos do PNCP e disputa.")

if __name__ == "__main__":
    run_db_migrations()
    print("Migrações executadas com sucesso.")
