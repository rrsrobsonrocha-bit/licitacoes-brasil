"""
Importação automática do pacote de habilitação por CNPJ (Lei 14.133/2021, arts. 62 a 70).

Só entra no cofre como documento válido o que foi de fato consultado numa fonte oficial:
- Situação cadastral do CNPJ (Receita Federal, via BrasilAPI): relatório em PDF com os dados
  da consulta de hoje; válido apenas se a situação for ATIVA.
- Sanções CEIS/CNEP (Portal da Transparência): relatório "nada consta", quando a consulta
  automática está configurada (PORTAL_TRANSPARENCIA_API_KEY) e não há registros.

CND federal (PGFN/Receita), CRF do FGTS e CNDT exigem emissão nos sites oficiais (com captcha):
entram como PENDENTES, com o link de emissão e a validade padrão sugerida para quando o PDF
for anexado. Nenhuma certidão é marcada como válida sem ter sido emitida.
"""
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import escape

from reportlab.platypus import Paragraph, Spacer
from sqlalchemy.orm import Session

from server.models import DocumentoEmpresa, hoje_sao_paulo
from server.services import document_vault, dossie
from server.services.pdf_generator import estilos_timbrados, renderizar_pdf_timbrado, tabela_ficha

ORIGEM_CNPJ = "CNPJ"

# chave -> definição. ``link`` recebe o CNPJ (14 dígitos).
CERTIDOES: List[Dict[str, Any]] = [
    {
        "chave": "cartao_cnpj",
        "nome": "Comprovante de Inscrição e Situação Cadastral (Cartão CNPJ)",
        "categoria": "juridica",
        "validade_dias": 180,
        "tags": "Habilitação Jurídica, Art. 66, CNPJ",
        "link": "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/Cnpjreva_Solicitacao.asp?cnpj={cnpj}",
    },
    {
        "chave": "cnd_federal",
        "nome": "Certidão Negativa de Débitos Federais e Dívida Ativa (PGFN/Receita)",
        "categoria": "fiscal",
        "validade_dias": 180,
        "tags": "Regularidade Fiscal, Art. 68, CND Federal",
        "link": "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj",
    },
    {
        "chave": "crf_fgts",
        "nome": "Certificado de Regularidade do FGTS (CRF Caixa)",
        "categoria": "fiscal",
        "validade_dias": 30,
        "tags": "Regularidade Social, Art. 68, FGTS",
        "link": "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf",
    },
    {
        "chave": "cndt",
        "nome": "Certidão Negativa de Débitos Trabalhistas (CNDT - TST)",
        "categoria": "fiscal",
        "validade_dias": 180,
        "tags": "Regularidade Trabalhista, Art. 68, CNDT",
        "link": "https://cndt-certidao.tst.jus.br/inicio.faces",
    },
    {
        "chave": "sancoes",
        "nome": "Certidão Negativa de Sanções e Inidoneidade (CEIS/CNEP/TCU)",
        "categoria": "juridica",
        "validade_dias": 30,
        "tags": "Habilitação Jurídica, Sanções, CEIS, CNEP, TCU",
        "link": "https://certidoes-apf.apps.tcu.gov.br/",
    },
]


def _ja_no_cofre(db: Session, user_id: Optional[str], cnpj: str, nome: str, hoje: date) -> Optional[DocumentoEmpresa]:
    """Mesmo documento deste CNPJ ainda pendente ou dentro da validade (reimportar não duplica)."""
    candidatos = db.query(DocumentoEmpresa).filter(
        DocumentoEmpresa.user_id.is_(None) if user_id is None else DocumentoEmpresa.user_id == user_id,
        DocumentoEmpresa.cnpj == cnpj,
        DocumentoEmpresa.nome == nome,
    ).all()
    for doc in candidatos:
        if doc.pendente or doc.status_validade(hoje) != "VENCIDO":
            return doc
    return None


def _pdf_situacao_cadastral(receita: Dict[str, Any], hoje: date) -> bytes:
    estilos = estilos_timbrados()
    end = receita["endereco"]
    endereco = ", ".join(p for p in (end["logradouro"], end["numero"], end["complemento"], end["bairro"]) if p)
    linhas = [
        ("CNPJ", receita["cnpj_formatado"]),
        ("Razão social", receita["razao_social"]),
        ("Nome fantasia", receita["nome_fantasia"] or "—"),
        ("Situação cadastral", f"{receita['situacao']}" + (f" desde {_br(receita['data_situacao'])}" if receita.get("data_situacao") else "")),
        ("Abertura", _br(receita.get("data_abertura")) or "—"),
        ("Natureza jurídica", receita["natureza_juridica"] or "—"),
        ("Porte", receita["porte"] or "—"),
        ("Atividade principal", f"{receita['cnae_principal']['codigo']} — {receita['cnae_principal']['descricao']}"),
        ("Endereço", f"{endereco} — {end['municipio']}/{end['uf']} — CEP {end['cep']}"),
    ]
    story = [
        Paragraph("SITUAÇÃO CADASTRAL DO CNPJ", estilos["titulo"]),
        Paragraph(f"Consulta à base da Receita Federal (via BrasilAPI) em {hoje:%d/%m/%Y}", estilos["subtitulo"]),
        tabela_ficha([(escape(r), escape(v)) for r, v in linhas], estilos),
        Spacer(1, 10),
        Paragraph(
            "Relatório gerado pela Licitações Brasil a partir dos dados públicos da Receita Federal. "
            "Quando o edital exigir o comprovante oficial, emita-o em "
            "solucoes.receita.fazenda.gov.br (Comprovante de Inscrição e de Situação Cadastral).",
            estilos["pequeno"]),
    ]
    return renderizar_pdf_timbrado(story, titulo_documento="Situação Cadastral do CNPJ",
                                   referencia=receita["cnpj_formatado"])


def _pdf_sancoes(receita: Dict[str, Any], sancoes: Dict[str, Any], hoje: date) -> bytes:
    estilos = estilos_timbrados()
    story = [
        Paragraph("CONSULTA DE SANÇÕES — CEIS / CNEP", estilos["titulo"]),
        Paragraph(f"Portal da Transparência (CGU) — consulta em {hoje:%d/%m/%Y}", estilos["subtitulo"]),
        tabela_ficha([
            ("CNPJ", escape(receita["cnpj_formatado"])),
            ("Razão social", escape(receita["razao_social"])),
            ("Resultado", "NADA CONSTA no Cadastro de Empresas Inidôneas e Suspensas (CEIS) "
                          "e no Cadastro Nacional de Empresas Punidas (CNEP)."),
        ], estilos),
        Spacer(1, 10),
        Paragraph("A consulta consolidada do TCU (inidôneos, CNIA/CNJ) pode ser emitida em "
                  "certidoes-apf.apps.tcu.gov.br.", estilos["pequeno"]),
    ]
    return renderizar_pdf_timbrado(story, titulo_documento="Consulta de Sanções CEIS/CNEP",
                                   referencia=receita["cnpj_formatado"])


def _br(iso: Optional[str]) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


def importar_por_cnpj(
    db: Session,
    cnpj: str,
    user_id: Optional[str],
    *,
    consultar_receita: Callable[[str], Dict[str, Any]] = None,
    consultar_sancoes: Callable[[str], Dict[str, Any]] = None,
    hoje: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Cria no cofre da conta o pacote de habilitação do CNPJ. Devolve o cadastro consultado e,
    por certidão, o que foi feito: ``valido`` (consulta real em PDF), ``pendente`` (emitir no
    site oficial), ``existente`` (já estava no cofre) ou ``alerta`` (consulta com restrição).
    """
    consultar_receita = consultar_receita or dossie.consultar_receita
    consultar_sancoes = consultar_sancoes or dossie.consultar_sancoes
    cnpj = dossie.normalizar_cnpj(cnpj)
    hoje = hoje or hoje_sao_paulo()
    receita = consultar_receita(cnpj)
    sancoes = consultar_sancoes(cnpj)
    ativa = receita.get("situacao") == "ATIVA"

    resultados: List[Dict[str, Any]] = []
    arquivos_gravados: List[str] = []
    try:
        for definicao in CERTIDOES:
            nome, link = definicao["nome"], definicao["link"].format(cnpj=cnpj)
            existente = _ja_no_cofre(db, user_id, cnpj, nome, hoje)
            if existente:
                resultados.append({"chave": definicao["chave"], "nome": nome, "resultado": "existente",
                                   "documento_id": existente.id})
                continue

            pdf, detalhe = None, ""
            if definicao["chave"] == "cartao_cnpj":
                if ativa:
                    pdf = _pdf_situacao_cadastral(receita, hoje)
                else:
                    detalhe = f"Situação cadastral {receita.get('situacao') or 'não informada'}: regularize na Receita."
            elif definicao["chave"] == "sancoes":
                if sancoes.get("verificado") and not sancoes.get("registros"):
                    pdf = _pdf_sancoes(receita, sancoes, hoje)
                elif sancoes.get("verificado"):
                    detalhe = f"{len(sancoes['registros'])} registro(s) de sanção no CEIS/CNEP."
                else:
                    link = sancoes.get("link") or link
                    detalhe = sancoes.get("motivo") or ""

            doc = DocumentoEmpresa(
                nome=nome,
                categoria=definicao["categoria"],
                data_emissao=hoje,  # pendente: data do registro; passa a ser a de emissão ao anexar o PDF
                data_validade=hoje + timedelta(days=definicao["validade_dias"]) if pdf else None,
                caminho_arquivo="",
                tags_habilitacao=document_vault.normalizar_tags(definicao["tags"]),
                user_id=user_id,
                cnpj=cnpj,
                origem=ORIGEM_CNPJ,
                link_oficial=link,
                validade_padrao_dias=definicao["validade_dias"],
                pendente=pdf is None,
            )
            if pdf:
                doc.caminho_arquivo = document_vault.salvar_pdf_no_cofre(pdf, nome)
                arquivos_gravados.append(doc.caminho_arquivo)
            db.add(doc)
            db.flush()
            com_restricao = (definicao["chave"] == "cartao_cnpj" and not ativa) or (
                definicao["chave"] == "sancoes" and sancoes.get("verificado") and sancoes.get("registros"))
            resultado = "valido" if pdf else ("alerta" if com_restricao else "pendente")
            resultados.append({"chave": definicao["chave"], "nome": nome, "resultado": resultado,
                               "documento_id": doc.id, "link_oficial": link, "detalhe": detalhe})
        db.commit()
    except Exception:
        db.rollback()
        for caminho in arquivos_gravados:
            document_vault.remover_arquivo_documento(caminho)
        raise

    return {
        "cnpj": cnpj,
        "cnpj_formatado": receita["cnpj_formatado"],
        "razao_social": receita["razao_social"],
        "situacao": receita["situacao"],
        "certidoes": resultados,
        "resumo": {r: sum(1 for x in resultados if x["resultado"] == r)
                   for r in ("valido", "pendente", "alerta", "existente")},
    }
