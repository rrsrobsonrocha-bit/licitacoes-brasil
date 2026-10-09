import io
import os
import hashlib
from typing import Dict, Any, List, Optional
import pypdf
from server.security import calculate_sha256, sanitize_tender_text_for_ai

class PDFProcessingResult:
    def __init__(
        self,
        success: bool,
        page_count: int = 0,
        checksum: str = "",
        is_scanned: bool = False,
        pages_text: List[Dict[str, Any]] = None,
        full_text: str = "",
        error_message: Optional[str] = None
    ):
        self.success = success
        self.page_count = page_count
        self.checksum = checksum
        self.is_scanned = is_scanned
        self.pages_text = pages_text or []
        self.full_text = full_text
        self.error_message = error_message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "page_count": self.page_count,
            "checksum": self.checksum,
            "is_scanned": self.is_scanned,
            "pages_count_extracted": len(self.pages_text),
            "full_text_length": len(self.full_text),
            "error_message": self.error_message
        }

def process_pdf_bytes(pdf_bytes: bytes, max_pages: int = 50, max_size_mb: int = 35) -> PDFProcessingResult:
    """
    Pipeline seguro de extração e validação de PDFs de Editais:
    1. Valida tamanho máximo de arquivo (prevenção contra DoS de memória).
    2. Valida Magic Bytes / cabeçalho (%PDF-).
    3. Calcula hash SHA-256 para integridade e detecção de duplicatas.
    4. Extrai texto página por página associando explicitamente o número da página.
    5. Detecta se o PDF é digitalizado/escaneado (baseado na densidade de caracteres).
    6. Sanitiza comandos de prompt injection antes de expor aos modelos de IA.
    """
    size_mb = len(pdf_bytes) / (1024 * 1024)
    if size_mb > max_size_mb:
        return PDFProcessingResult(
            success=False,
            error_message=f"Arquivo excede o limite máximo permitido de {max_size_mb} MB (tamanho: {size_mb:.2f} MB)."
        )

    # Validar cabeçalho PDF (%PDF-)
    if not pdf_bytes.startswith(b"%PDF-"):
        return PDFProcessingResult(
            success=False,
            error_message="Formato inválido. O arquivo não é um documento PDF válido (cabeçalho %PDF ausente)."
        )

    checksum = calculate_sha256(pdf_bytes)

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        if total_pages == 0:
            return PDFProcessingResult(
                success=False,
                checksum=checksum,
                error_message="O documento PDF não contém páginas."
            )

        pages_to_extract = min(total_pages, max_pages)
        extracted_pages: List[Dict[str, Any]] = []
        combined_text_chunks: List[str] = []
        total_chars_extracted = 0

        for i in range(pages_to_extract):
            page_num = i + 1
            try:
                page = reader.pages[i]
                raw_text = page.extract_text() or ""
                # Sanitização ativa contra prompt injection
                clean_text = sanitize_tender_text_for_ai(raw_text.strip())
                char_count = len(clean_text)
                total_chars_extracted += char_count

                extracted_pages.append({
                    "page_number": page_num,
                    "text": clean_text,
                    "char_count": char_count
                })

                if clean_text:
                    combined_text_chunks.append(f"--- [PÁGINA {page_num}] ---\n{clean_text}\n")
            except Exception as pe:
                extracted_pages.append({
                    "page_number": page_num,
                    "text": "",
                    "char_count": 0,
                    "error": str(pe)
                })

        avg_chars_per_page = total_chars_extracted / max(1, pages_to_extract)
        # Se houver menos de 40 caracteres por página, é altamente provável ser documento escaneado/imagem
        is_scanned = avg_chars_per_page < 40 and total_pages > 0

        full_text = "\n".join(combined_text_chunks)

        return PDFProcessingResult(
            success=True,
            page_count=total_pages,
            checksum=checksum,
            is_scanned=is_scanned,
            pages_text=extracted_pages,
            full_text=full_text,
            error_message=None
        )

    except Exception as e:
        return PDFProcessingResult(
            success=False,
            checksum=checksum,
            error_message=f"Falha na leitura estrutural do PDF: {str(e)}"
        )

def chunk_text_with_citations(pages_text: List[Dict[str, Any]], max_chunk_chars: int = 12000) -> List[Dict[str, Any]]:
    """
    Divide o texto extraído em blocos prontos para o LLM preservando o intervalo de páginas citado.
    """
    chunks = []
    current_chunk = []
    current_length = 0
    start_page = 1
    current_page = 1

    for page_data in pages_text:
        page_num = page_data["page_number"]
        page_text = page_data.get("text", "")
        if not page_text:
            continue

        formatted_page = f"--- [PÁGINA {page_num}] ---\n{page_text}\n"
        if current_length + len(formatted_page) > max_chunk_chars and current_chunk:
            chunks.append({
                "start_page": start_page,
                "end_page": current_page,
                "content": "".join(current_chunk)
            })
            current_chunk = [formatted_page]
            current_length = len(formatted_page)
            start_page = page_num
        else:
            if not current_chunk:
                start_page = page_num
            current_chunk.append(formatted_page)
            current_length += len(formatted_page)
        current_page = page_num

    if current_chunk:
        chunks.append({
            "start_page": start_page,
            "end_page": current_page,
            "content": "".join(current_chunk)
        })

    return chunks
