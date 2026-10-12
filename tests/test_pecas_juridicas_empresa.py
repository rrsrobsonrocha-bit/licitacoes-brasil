"""Peças jurídicas preenchidas com a empresa ativa da conta e com os dados do certame."""
import io
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from server.database import SessionLocal
from server.main import app
from server.models import EmpresaUsuario, LegalDocumentTemplate, Tender
from server.services import dossie
from server.services.pdf_generator import preencher_peca

client = TestClient(app)
CNPJ = "11222333000181"
MARCADORES_LICITANTE = ("[CNPJ]", "[RAZÃO SOCIAL DA LICITANTE]", "[ENDEREÇO COMPLETO]", "[ENDEREÇO]",
                        "[NOME DA EMPRESA REQUERENTE]", "[NOME DA EMPRESA]", "[REPRESENTANTE LEGAL]",
                        "[RECORRENTE]", "[RECORRIDA]")
RECEITA = {
    "cnpj": CNPJ, "razao_social": "Alfa Distribuidora Hospitalar Ltda", "descricao_situacao_cadastral": "ATIVA",
    "descricao_tipo_de_logradouro": "RUA", "logradouro": "DAS FLORES", "numero": "100", "bairro": "CENTRO",
    "municipio": "MACEIO", "uf": "AL", "cep": "57020000", "email": "juridico@alfa.com.br", "ddd_telefone_1": "8233334444",
    "qsa": [{"nome_socio": "MARIA ADMINISTRADORA", "qualificacao_socio": "Sócio-Administrador"}],
}


@pytest.fixture
def conta_com_empresa(criar_usuario, monkeypatch):
    monkeypatch.setattr(dossie, "requisitar_json", lambda url, params=None, headers=None: RECEITA)
    conta = criar_usuario("Conta Jurídico")
    assert client.post("/api/minhas-empresas", headers=conta.headers, json={"cnpj": CNPJ}).status_code == 201
    yield conta
    db = SessionLocal()
    db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == conta.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def _modelos():
    db = SessionLocal()
    ids = [i for (i,) in db.query(LegalDocumentTemplate.id).order_by(LegalDocumentTemplate.id)]
    tender = db.query(Tender).filter(Tender.contracting_entity.isnot(None), Tender.title.isnot(None)).first()
    dados = {"ids": ids, "tender_id": tender.id, "orgao": tender.contracting_entity, "objeto": tender.title}
    db.close()
    return dados


def test_todas_as_pecas_saem_com_a_empresa_ativa_e_o_certame(conta_com_empresa):
    m = _modelos()
    assert m["ids"]
    for template_id in m["ids"]:
        resp = client.post("/api/juridico/preencher-peca", headers=conta_com_empresa.headers,
                           json={"template_id": template_id, "tender_id": m["tender_id"]})
        assert resp.status_code == 200, resp.text
        dados = resp.json()
        texto = dados["corpo"]
        for marcador in MARCADORES_LICITANTE:
            assert marcador not in texto, (template_id, marcador)
        assert "{{orgao}}" not in texto and "{{numero_edital}}" not in texto and "{{objeto}}" not in texto
        assert m["orgao"] in texto
        assert dados["empresa_ativa"]["cnpj"] == CNPJ
        assert not any(p in dados["pendentes"] for p in MARCADORES_LICITANTE)

    impugnacao = client.post("/api/juridico/preencher-peca", headers=conta_com_empresa.headers,
                             json={"template_id": m["ids"][0], "tender_id": m["tender_id"]}).json()["corpo"]
    assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA, inscrita no CNPJ sob o nº 11.222.333/0001-81" in impugnacao
    assert "com sede em RUA DAS FLORES, 100, CENTRO – MACEIO/AL – CEP 57020-000" in impugnacao
    assert re.search(r"MACEIO/AL, \d{1,2} de \w+ de \d{4}\.", impugnacao)  # [CIDADE - UF], [DATA]


def test_pdf_da_peca_sem_marcadores_da_licitante(conta_com_empresa):
    m = _modelos()
    resp = client.post("/api/juridico/gerar-pdf", headers=conta_com_empresa.headers,
                       json={"template_id": m["ids"][0], "tender_id": m["tender_id"]})
    assert resp.status_code == 200, resp.text
    texto = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(resp.content)).pages)
    for marcador in ("[CNPJ]", "[RAZÃO SOCIAL DA LICITANTE]", "[ENDEREÇO COMPLETO]", "[NOME DA EMPRESA REQUERENTE]",
                     "[ENDEREÇO]", "[REPRESENTANTE LEGAL]", "[DATA]"):
        assert marcador not in texto, marcador
    assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in texto and "11.222.333/0001-81" in texto
    assert "MARIA ADMINISTRADORA" in texto
    assert m["orgao"].split()[0] in texto


def test_dados_informados_no_modal_prevalecem_sobre_o_cadastro(conta_com_empresa):
    resp = client.post("/api/juridico/preencher-peca", headers=conta_com_empresa.headers, json={
        "corpo": "[NOME DA EMPRESA], representada por [REPRESENTANTE LEGAL], certame [NÚMERO DO CERTAME] do [ÓRGÃO COMPRADOR]: [OBJETO DA LICITAÇÃO].",
        "licitante": {"representante": "João Procurador"}, "numero_certame": "PE 77/2026",
        "orgao": "Prefeitura de Exemplo", "objeto": "Notebooks"})
    assert resp.json()["corpo"] == ("ALFA DISTRIBUIDORA HOSPITALAR LTDA, representada por João Procurador, certame "
                                   "PE 77/2026 do Prefeitura de Exemplo: Notebooks.")


def test_sem_empresa_cadastrada_mostra_aviso_e_mantem_marcadores(criar_usuario, conta_com_empresa):
    sem_empresa = criar_usuario("Sem empresa")
    dados = client.post("/api/juridico/preencher-peca", headers=sem_empresa.headers,
                        json={"corpo": "[NOME DA EMPRESA REQUERENTE], CNPJ [CNPJ]"}).json()
    assert dados["empresa_ativa"] is None and "[CNPJ]" in dados["pendentes"]
    assert "ALFA" not in dados["corpo"]  # nunca usa a empresa de outra conta
    html = client.get("/juridico", headers=sem_empresa.headers).text
    aviso = "Cadastre seu CNPJ no Cofre ou Perfil para gerar petições personalizadas."
    assert aviso in html and 'class="aviso-sem-empresa bg-amber' in html and 'id="cnpj-rapido-modal"' in html
    com_empresa = client.get("/juridico", headers=conta_com_empresa.headers).text
    assert 'class="aviso-sem-empresa hidden' in com_empresa
    assert "Peças geradas em nome de" in com_empresa and "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in com_empresa.upper()


def test_preencher_peca_unitario():
    texto = preencher_peca(
        "[RAZÃO SOCIAL DA LICITANTE] ([CNPJ]) – [ENDEREÇO COMPLETO] – [E-MAIL] / [TELEFONE]\n[CIDADE - UF], [DATA].",
        licitante={"razao_social": r"Beta \1 & $ Ltda", "cnpj": "11.222.333/0001-81", "endereco": "Rua B, 2",
                   "email": "a@b.com", "telefone": "(82) 3333-4444", "cidade_uf": "Arapiraca/AL"},
        data_documento=date(2026, 10, 11))
    assert texto == (r"BETA \1 & $ LTDA (11.222.333/0001-81) – Rua B, 2 – a@b.com / (82) 3333-4444" "\n"
                     "Arapiraca/AL, 11 de outubro de 2026.")
    # No PDF, a linha de local/data sai do corpo (a assinatura do PDF já traz local e data)
    assert preencher_peca("Pede deferimento.\n[CIDADE - UF], [DATA].", remover_fecho=True) == "Pede deferimento."
