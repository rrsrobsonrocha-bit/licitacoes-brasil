"""
Cache em memória das métricas agregadas do catálogo global (painel, busca e fornecedores).

Os COUNTs e agregações sobre dezenas de milhares de editais e milhares de órgãos são iguais para
todas as contas e só mudam quando a coleta grava editais novos; por isso ficam guardados por
``METRICAS_CACHE_TTL`` segundos (padrão: 15 min) e são descartados ao fim de cada sincronização.
Dados privados (funil, favoritos, alertas) nunca passam por aqui.
"""
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

TTL_PADRAO = 15 * 60

_lock = threading.Lock()
_entradas: Dict[str, Tuple[float, Any]] = {}


def ttl_segundos() -> int:
    try:
        return max(0, int(os.getenv("METRICAS_CACHE_TTL", TTL_PADRAO)))
    except ValueError:
        return TTL_PADRAO


def obter(chave: str, calcular: Callable[[], Any], ttl: Optional[int] = None) -> Any:
    """Valor guardado de ``chave`` enquanto não expira; senão recalcula e guarda (TTL 0 desliga o cache)."""
    ttl = ttl_segundos() if ttl is None else ttl
    if ttl <= 0:
        return calcular()
    agora = time.monotonic()
    with _lock:
        entrada = _entradas.get(chave)
        if entrada and entrada[0] > agora:
            return entrada[1]
    valor = calcular()  # fora do lock: uma consulta lenta não trava as demais chaves
    with _lock:
        _entradas[chave] = (time.monotonic() + ttl, valor)
    return valor


def invalidar(prefixo: str = "") -> None:
    """Descarta as entradas (todas, ou as que começam com ``prefixo``): chamado após cada coleta."""
    with _lock:
        for chave in [c for c in _entradas if c.startswith(prefixo)]:
            del _entradas[chave]
