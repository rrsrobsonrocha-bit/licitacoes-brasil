"""
Cache em memória com TTL para contagens globais caras (COUNT em tabelas grandes).

Durante a coleta em segundo plano o banco recebe gravações contínuas; servir a última contagem
por alguns segundos evita consultas repetidas e telas com "--" por timeout.
"""
import threading
import time
from typing import Any, Callable, Dict, Tuple

_trava = threading.Lock()
_valores: Dict[str, Tuple[float, Any]] = {}


def em_cache(chave: str, ttl_segundos: float, calcular: Callable[[], Any]) -> Any:
    """Devolve o valor guardado se ainda dentro do TTL; senão recalcula e guarda. TTL <= 0 desliga."""
    if ttl_segundos <= 0:
        return calcular()
    agora = time.monotonic()
    with _trava:
        guardado = _valores.get(chave)
        if guardado and agora - guardado[0] < ttl_segundos:
            return guardado[1]
    valor = calcular()
    with _trava:
        _valores[chave] = (time.monotonic(), valor)
    return valor


def limpar_cache() -> None:
    with _trava:
        _valores.clear()
