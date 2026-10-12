"""
Rotação de chaves da API do Gemini (round-robin com fallback por cota).

- Chaves: ``GEMINI_API_KEYS`` (várias, separadas por vírgula); sem ela, a antiga ``GEMINI_API_KEY``
  (e os apelidos ``GOOGLE_API_KEY`` / ``AI_API_KEY``).
- Cada pedido começa pela próxima chave do ciclo, para distribuir a carga entre elas.
- Erro de cota (HTTP 429 / RESOURCE_EXHAUSTED) deixa a chave em espera por alguns segundos e o mesmo
  pedido é refeito na hora com a chave seguinte. Só quando TODAS falham por cota no mesmo ciclo o
  erro sobe (``CotaGeminiEsgotada``, que os chamadores tratam como erro transitório: outro modelo,
  nova tentativa ou o fallback por regras). Outros erros (modelo inexistente, rede, JSON) sobem na
  hora: não são culpa da chave.

``ClienteRotativo`` tem a mesma interface usada do ``google.genai.Client``
(``models.generate_content`` e ``aio.models.generate_content``), então os serviços de IA não mudam.
"""
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ESPERA_COTA_PADRAO = 60.0  # segundos que uma chave esgotada fica fora do ciclo
VARIAVEIS_CHAVE_UNICA = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY")


class CotaGeminiEsgotada(Exception):
    """Todas as chaves do ciclo devolveram erro de cota."""


def chaves_configuradas() -> List[str]:
    """Chaves válidas, sem repetição, na ordem configurada (lidas a cada uso: trocas no ambiente valem na hora)."""
    brutas = (os.getenv("GEMINI_API_KEYS") or "").split(",")
    if not any(c.strip() for c in brutas):
        brutas = [os.getenv(nome) or "" for nome in VARIAVEIS_CHAVE_UNICA]
        try:
            from server.config import settings
            brutas += [settings.GEMINI_API_KEY or "", settings.AI_API_KEY or ""]
        except Exception:
            pass
        brutas = [next((c for c in brutas if c and c.strip()), "")]  # chave única: a primeira definida
    return list(dict.fromkeys(c.strip() for c in brutas if c and c.strip()))


def tem_chaves() -> bool:
    return bool(chaves_configuradas())


def mascarar(chave: str) -> str:
    return f"…{chave[-4:]}" if len(chave) > 4 else "…"


def e_erro_de_cota(exc: BaseException) -> bool:
    """429 / RESOURCE_EXHAUSTED do google-genai, ResourceExhausted/TooManyRequests do google.api_core ou equivalentes."""
    if getattr(exc, "code", None) == 429 or getattr(exc, "status_code", None) == 429:
        return True
    if "RESOURCE_EXHAUSTED" in str(getattr(exc, "status", "") or "").upper():
        return True
    try:
        from google.api_core import exceptions as gexc
        if isinstance(exc, (gexc.ResourceExhausted, gexc.TooManyRequests)):
            return True
    except ImportError:
        pass
    texto = f"{type(exc).__name__} {exc}".upper()
    return any(m in texto for m in ("429", "RESOURCE_EXHAUSTED", "RESOURCEEXHAUSTED", "TOO MANY REQUESTS", "QUOTA"))


def _espera_sugerida(exc: BaseException) -> float:
    """Usa o retryDelay devolvido pelo Google (ex.: "retryDelay": "37s") quando houver."""
    achado = re.search(r"retry[_ ]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc), re.IGNORECASE)
    try:
        padrao = float(os.getenv("GEMINI_ESPERA_COTA_SEGUNDOS", ESPERA_COTA_PADRAO))
    except ValueError:
        padrao = ESPERA_COTA_PADRAO
    return float(achado.group(1)) if achado else padrao


class _EstadoRotacao:
    """Estado global do processo: posição do ciclo e chaves em espera por cota."""

    def __init__(self, relogio: Callable[[], float] = time.monotonic):
        self.relogio = relogio
        self._lock = threading.Lock()
        self._cursor = 0
        self._esgotada_ate: Dict[str, float] = {}

    def ordem(self, chaves: List[str]) -> List[str]:
        """Chaves para um pedido: começa na próxima do round-robin; as em espera vão para o fim."""
        if not chaves:
            return []
        with self._lock:
            inicio = self._cursor % len(chaves)
            self._cursor = (self._cursor + 1) % len(chaves)
            agora = self.relogio()
            ciclo = chaves[inicio:] + chaves[:inicio]
        livres = [c for c in ciclo if self._esgotada_ate.get(c, 0) <= agora]
        # Todas em espera: tenta mesmo assim (a cota pode ter voltado antes do previsto)
        return livres + [c for c in ciclo if c not in livres]

    def marcar_esgotada(self, chave: str, segundos: float):
        with self._lock:
            self._esgotada_ate[chave] = self.relogio() + segundos

    def reiniciar(self):
        with self._lock:
            self._cursor = 0
            self._esgotada_ate.clear()


estado = _EstadoRotacao()


def _novo_cliente(chave: str):
    import google.genai as genai
    return genai.Client(api_key=chave)


class ClienteRotativo:
    """Cliente do Gemini que troca de chave quando a atual esgota a cota (mesma interface do genai.Client)."""

    def __init__(self, chaves: Optional[List[str]] = None, fabrica: Callable[[str], Any] = _novo_cliente):
        self.chaves = list(chaves) if chaves is not None else chaves_configuradas()
        self._fabrica = fabrica
        self._clientes: Dict[str, Any] = {}
        self.models = _ModelosSync(self)
        self.aio = _Aio(self)

    def _cliente(self, chave: str):
        if chave not in self._clientes:
            self._clientes[chave] = self._fabrica(chave)
        return self._clientes[chave]

    def _falhou_por_cota(self, chave: str, exc: BaseException):
        espera = _espera_sugerida(exc)
        estado.marcar_esgotada(chave, espera)
        logger.warning("Gemini: cota esgotada na chave %s (%s); em espera por %.0f s, tentando a próxima.",
                       mascarar(chave), type(exc).__name__, espera)

    def _sem_chaves(self, erros: List[BaseException]):
        if not self.chaves:
            raise CotaGeminiEsgotada("Nenhuma chave do Gemini configurada (GEMINI_API_KEYS / GEMINI_API_KEY).")
        raise CotaGeminiEsgotada(
            f"429 RESOURCE_EXHAUSTED: todas as {len(self.chaves)} chave(s) do Gemini sem cota neste ciclo "
            f"(último erro: {erros[-1]!r})") from erros[-1]

    def executar(self, chamada: Callable[[Any], Any]):
        erros: List[BaseException] = []
        for chave in estado.ordem(self.chaves):
            try:
                return chamada(self._cliente(chave))
            except Exception as exc:
                if not e_erro_de_cota(exc):
                    raise
                erros.append(exc)
                self._falhou_por_cota(chave, exc)
        self._sem_chaves(erros)

    async def executar_async(self, chamada: Callable[[Any], Any]):
        erros: List[BaseException] = []
        for chave in estado.ordem(self.chaves):
            try:
                return await chamada(self._cliente(chave))
            except Exception as exc:
                if not e_erro_de_cota(exc):
                    raise
                erros.append(exc)
                self._falhou_por_cota(chave, exc)
        self._sem_chaves(erros)


class _ModelosSync:
    def __init__(self, rotativo: ClienteRotativo):
        self._rotativo = rotativo

    def generate_content(self, *args, **kwargs):
        return self._rotativo.executar(lambda c: c.models.generate_content(*args, **kwargs))


class _ModelosAsync:
    def __init__(self, rotativo: ClienteRotativo):
        self._rotativo = rotativo

    async def generate_content(self, *args, **kwargs):
        return await self._rotativo.executar_async(lambda c: c.aio.models.generate_content(*args, **kwargs))


class _Aio:
    def __init__(self, rotativo: ClienteRotativo):
        self.models = _ModelosAsync(rotativo)


def cliente_rotativo() -> Optional[ClienteRotativo]:
    """Cliente com todas as chaves configuradas, ou None sem nenhuma chave (os serviços usam o fallback)."""
    chaves = chaves_configuradas()
    return ClienteRotativo(chaves) if chaves else None
