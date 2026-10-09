import pytest
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from server.pdf_processor import process_pdf_bytes, chunk_text_with_citations

def create_sample_pdf_bytes(text_page_1: str, text_page_2: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.drawString(100, 750, text_page_1)
    c.showPage()
    c.drawString(100, 750, text_page_2)
    c.showPage()
    c.save()
    return buffer.getvalue()

def test_pdf_processing_pipeline():
    sample_pdf = create_sample_pdf_bytes(
        "EDITAL DE PREGÃO ELETRÔNICO Nº 042/2025 - AQUISIÇÃO DE MEDICAMENTOS",
        "CLÁUSULA SEGUNDA - DOS REQUISITOS DE HABILITAÇÃO TÉCNICA ANVISA"
    )

    result = process_pdf_bytes(sample_pdf)
    assert result.success is True
    assert result.page_count == 2
    assert len(result.checksum) == 64
    assert result.is_scanned is False
    assert len(result.pages_text) == 2
    assert "MEDICAMENTOS" in result.full_text

def test_pdf_invalid_bytes():
    fake_bytes = b"ESTE_NAO_E_UM_PDF_VALIDO"
    result = process_pdf_bytes(fake_bytes)
    assert result.success is False
    assert "não é um documento PDF válido" in result.error_message

def test_pdf_chunking_with_citations():
    pages_data = [
        {"page_number": 1, "text": "Objeto do certame e disposições iniciais."},
        {"page_number": 2, "text": "Critérios de qualificação econômico-financeira."}
    ]
    chunks = chunk_text_with_citations(pages_data, max_chunk_chars=5000)
    assert len(chunks) == 1
    assert chunks[0]["start_page"] == 1
    assert chunks[0]["end_page"] == 2
    assert "[PÁGINA 1]" in chunks[0]["content"]
    assert "[PÁGINA 2]" in chunks[0]["content"]
