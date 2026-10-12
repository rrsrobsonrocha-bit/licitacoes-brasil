"""
Robô de Lances Desktop - Licitações Brasil.

Motor local que roda na máquina do administrador e envia os lances ao portal de compras, enquanto o
sistema web guarda a estratégia (piso/degrau), o Kill-Switch e a auditoria de cada lance.

Travas inegociáveis:
- Stop Loss: nenhum lance abaixo do preço mínimo sai do processo. A checagem acontece no cálculo E
  no adaptador do portal, antes de qualquer byte de rede; violada, a execução é abortada.
- Kill-Switch: o loop para em até ~50 ms quando o painel web aciona o Kill-Switch (recebido no
  sinal de vida) ou quando o operador aperta ESPAÇO ou Q no terminal.
- Sem contato com o servidor por mais de 6 s: os lances ficam suspensos até a conexão voltar.

Uso: RoboLances_LicitacoesBrasil.exe [--config caminho/config.json]
Códigos de saída: 0 encerrado normalmente, 1 erro, 2 Kill-Switch/parada manual, 3 Stop Loss.
"""
import argparse
import getpass
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Optional

VERSAO = "1.0.0"
HEARTBEAT_SEGUNDOS = 1.5
LIMITE_SEM_CONTATO_SEGUNDOS = 6.0
TICK_SEGUNDOS = 0.05          # granularidade do loop: Kill-Switch atendido em até ~50 ms
TIMEOUT_HTTP_SEGUNDOS = 3.0
INTERVALO_MINIMO_PADRAO = 20  # segundos entre lances próprios (confira a regra do portal/edital)
INTERVALO_MINIMO_ABSOLUTO = 3
CENTAVO = Decimal("0.01")

SAIDA_OK, SAIDA_ERRO, SAIDA_KILL, SAIDA_STOP_LOSS = 0, 1, 2, 3


class StopLossViolado(Exception):
    """Um cálculo ou ordem resultou em lance abaixo do preço mínimo: a execução é abortada."""


class RoboAbortado(Exception):
    """Kill-Switch do painel web ou parada manual no terminal."""


def centavos(valor: Any) -> Decimal:
    try:
        numero = valor if isinstance(valor, Decimal) else Decimal(str(valor).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"Valor monetário inválido: {valor!r}")
    if not numero.is_finite():
        raise ValueError(f"Valor monetário inválido: {valor!r}")
    return numero.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def trava_stop_loss(valor: Any, piso: Any) -> Decimal:
    """Trava única de Stop Loss: sem piso definido ou lance abaixo dele, nada é enviado."""
    lance, minimo = centavos(valor), centavos(piso)
    if minimo <= 0:
        raise StopLossViolado("Preço mínimo (piso) não definido no painel: o robô não opera sem piso.")
    if lance < minimo:
        raise StopLossViolado(f"Lance de R$ {lance} abaixo do preço mínimo de R$ {minimo}.")
    return lance


def calcular_proximo_lance(melhor_lance: Any, meu_lance: Any, decremento: Any, piso: Any) -> Optional[Decimal]:
    """
    Próximo lance para assumir a liderança: melhor lance concorrente menos o decremento mínimo.
    None quando não há o que cobrir (sem lance concorrente ou já liderando). Abaixo do piso:
    StopLossViolado (a execução é abortada, nunca "arredonda" para o piso).
    """
    melhor, meu, degrau = centavos(melhor_lance or 0), centavos(meu_lance or 0), centavos(decremento or 0)
    if melhor <= 0:
        return None
    if meu > 0 and meu <= melhor:
        return None
    if degrau <= 0:
        raise StopLossViolado("Decremento mínimo inválido (zero ou negativo).")
    return trava_stop_loss(melhor - degrau, piso)


# ------------------------------------------------------------------------------
# Comunicação com o sistema web (Licitações Brasil)
# ------------------------------------------------------------------------------
class ClienteBackend:
    """Rotas do worker em /api/disputa/{item}/... autenticadas com o token da conta ADMIN."""

    def __init__(self, url_base: str, token: str, item_id: str, sessao=None):
        self.url_base = url_base.rstrip("/")
        self.item_id = item_id
        if sessao is None:
            import requests
            sessao = requests.Session()
        self.sessao = sessao
        self.cabecalhos = {"Authorization": f"Bearer {token}", "User-Agent": f"RoboLances-LicitacoesBrasil/{VERSAO}"}

    def _url(self, sufixo: str) -> str:
        from urllib.parse import quote
        return f"{self.url_base}/api/disputa/{quote(self.item_id, safe='')}/{sufixo}"

    @staticmethod
    def _checar(resp) -> Dict[str, Any]:
        if resp.status_code in (401, 403):
            raise PermissionError("Acesso negado: o robô exige uma sessão válida de administrador.")
        if resp.status_code == 404:
            raise LookupError("Lote não encontrado no cockpit da sua conta (acompanhe-o em /disputa).")
        return resp.json()

    def config(self) -> Dict[str, Any]:
        resp = self.sessao.get(self._url("worker-config"), headers=self.cabecalhos, timeout=TIMEOUT_HTTP_SEGUNDOS)
        dados = self._checar(resp)
        if resp.status_code >= 400:
            raise ConnectionError(f"Configuração do lote indisponível ({resp.status_code}).")
        return dados

    def heartbeat(self, status: str, latencia_ms: int) -> Dict[str, Any]:
        resp = self.sessao.post(self._url("worker-heartbeat"), headers=self.cabecalhos, timeout=2.0,
                                json={"status": status, "latencia_ms": latencia_ms, "versao": VERSAO})
        dados = self._checar(resp)
        if resp.status_code >= 400:
            raise ConnectionError(f"Heartbeat recusado ({resp.status_code}).")
        return dados

    def registrar_lance(self, valor: Decimal, momento_ms: int, aceito: bool, resposta: str,
                        latencia_ms: int) -> Dict[str, Any]:
        resp = self.sessao.post(self._url("worker-log"), headers=self.cabecalhos, timeout=TIMEOUT_HTTP_SEGUNDOS,
                                json={"valor": str(valor), "timestamp_ms": momento_ms, "aceito": aceito,
                                      "resposta_portal": resposta[:500], "latencia_ms": latencia_ms})
        dados = self._checar(resp)
        if resp.status_code == 422 and isinstance(dados.get("detail"), dict) and dados["detail"].get("abortar"):
            raise StopLossViolado(dados["detail"].get("mensagem") or "Servidor recusou o lance (piso).")
        if resp.status_code >= 400:
            raise ConnectionError(f"Registro do lance recusado ({resp.status_code}).")
        return dados

    @staticmethod
    def login(url_base: str, email: str, senha: str, sessao=None) -> str:
        if sessao is None:
            import requests
            sessao = requests.Session()
        resp = sessao.post(url_base.rstrip("/") + "/api/auth/login", timeout=10,
                           json={"email": email, "password": senha, "remember_me": True})
        if resp.status_code != 200:
            raise PermissionError("E-mail ou senha inválidos.")
        return resp.json()["access_token"]


# ------------------------------------------------------------------------------
# Adaptadores de portal (o envio de lance propriamente dito)
# ------------------------------------------------------------------------------
@dataclass
class RespostaPortal:
    aceito: bool
    texto: str
    latencia_ms: int


class AdaptadorPortal:
    """Base: toda ordem passa pela trava de Stop Loss ANTES de qualquer acesso à rede."""
    nome = "base"
    envia_de_verdade = True

    def __init__(self, opcoes: Optional[Dict[str, Any]] = None):
        self.opcoes = opcoes or {}

    def ler_melhor_lance(self) -> Optional[Decimal]:
        """Melhor lance lido no portal; None = usar o informado no cockpit web (ranking/melhor lance)."""
        return None

    def enviar_lance(self, valor: Decimal, piso: Decimal) -> RespostaPortal:
        lance = trava_stop_loss(valor, piso)  # nenhum byte sai daqui abaixo do piso
        inicio = time.perf_counter()
        aceito, texto = self._enviar(lance)
        return RespostaPortal(aceito, texto, int((time.perf_counter() - inicio) * 1000))

    def _enviar(self, valor: Decimal):
        raise NotImplementedError


class AdaptadorSimulacao(AdaptadorPortal):
    """Modo padrão: percorre o fluxo inteiro (travas, auditoria, cockpit) sem enviar nada ao portal."""
    nome = "simulacao"
    envia_de_verdade = False

    def _enviar(self, valor: Decimal):
        return True, f"SIMULACAO: lance de R$ {valor} não enviado ao portal"


class AdaptadorHttp(AdaptadorPortal):
    """
    Envio por HTTP com a sessão autenticada do operador no portal (cookies/token copiados do navegador).
    O endpoint e o formato do lance de cada portal não são uma API pública: são informados em
    config.json ("portal": url_lance, metodo, headers, cookies, payload_modelo com "{valor}").
    """
    nome = "http"
    separador_decimal = "."

    def __init__(self, opcoes: Optional[Dict[str, Any]] = None, sessao=None):
        super().__init__(opcoes)
        if not self.opcoes.get("url_lance"):
            raise ValueError(f"Portal '{self.nome}': informe portal.url_lance em config.json.")
        if not isinstance(self.opcoes.get("payload_modelo"), dict):
            raise ValueError(f"Portal '{self.nome}': informe portal.payload_modelo (objeto JSON com \"{{valor}}\").")
        if sessao is None:
            import requests
            sessao = requests.Session()
        self.sessao = sessao
        self.sessao.headers.update(self.opcoes.get("headers") or {})
        if self.opcoes.get("token"):
            self.sessao.headers["Authorization"] = f"Bearer {self.opcoes['token']}"
        for nome, valor in (self.opcoes.get("cookies") or {}).items():
            self.sessao.cookies.set(nome, valor)

    def formatar(self, valor: Decimal) -> str:
        return f"{valor:.2f}".replace(".", self.separador_decimal)

    def montar_payload(self, valor: Decimal) -> Dict[str, Any]:
        texto = self.formatar(valor)
        return {k: (v.replace("{valor}", texto) if isinstance(v, str) else v)
                for k, v in self.opcoes["payload_modelo"].items()}

    def _enviar(self, valor: Decimal):
        resp = self.sessao.request(self.opcoes.get("metodo", "POST"), self.opcoes["url_lance"],
                                   json=self.montar_payload(valor), timeout=TIMEOUT_HTTP_SEGUNDOS)
        return 200 <= resp.status_code < 300, f"HTTP {resp.status_code}: {resp.text[:300]}"


class AdaptadorComprasNet(AdaptadorHttp):
    nome = "comprasnet"


class AdaptadorBNC(AdaptadorHttp):
    nome = "bnc"
    separador_decimal = ","


ADAPTADORES = {"simulacao": AdaptadorSimulacao, "comprasnet": AdaptadorComprasNet, "bnc": AdaptadorBNC}


def criar_adaptador(opcoes_portal: Dict[str, Any], portal_do_lote: str) -> AdaptadorPortal:
    modo = (opcoes_portal or {}).get("modo", "simulacao")
    if modo == "simulacao":
        return AdaptadorSimulacao(opcoes_portal)
    if modo != portal_do_lote:
        raise ValueError(f"config.json está em modo '{modo}', mas o lote é do portal '{portal_do_lote}'.")
    if modo not in ADAPTADORES:
        raise ValueError(f"Portal '{modo}' sem adaptador (use: {', '.join(ADAPTADORES)}).")
    return ADAPTADORES[modo](opcoes_portal)


# ------------------------------------------------------------------------------
# Motor da disputa
# ------------------------------------------------------------------------------
class Robo:
    def __init__(self, backend: ClienteBackend, adaptador: AdaptadorPortal,
                 intervalo_minimo: float = INTERVALO_MINIMO_PADRAO, relogio=time.monotonic, saida=print):
        self.backend = backend
        self.adaptador = adaptador
        self.intervalo_minimo = max(float(intervalo_minimo), INTERVALO_MINIMO_ABSOLUTO)
        self.relogio = relogio
        self.saida = saida
        self.parar = threading.Event()
        self.motivo_parada = ""
        self.config: Dict[str, Any] = {}
        self.ultimo_contato = 0.0
        self.latencia_ms = 0
        self.ultimo_lance_em: Optional[float] = None
        self.status = "INICIANDO"
        self.lances_enviados = 0
        self._lock = threading.Lock()

    # ---- parada ----------------------------------------------------------------
    def abortar(self, motivo: str):
        if not self.parar.is_set():
            self.motivo_parada = motivo
            self.parar.set()

    def _checar_parada(self):
        if self.parar.is_set():
            raise RoboAbortado(self.motivo_parada or "Parada solicitada.")

    # ---- handshake e sinal de vida --------------------------------------------
    def handshake(self) -> Dict[str, Any]:
        inicio = time.perf_counter()
        cfg = self.backend.config()
        self.latencia_ms = int((time.perf_counter() - inicio) * 1000)
        with self._lock:
            self.config = cfg
            self.ultimo_contato = self.relogio()
        if cfg.get("kill_switch"):
            self.abortar("Kill-Switch já acionado no painel web.")
        return cfg

    def pulso(self):
        """Um sinal de vida: atualiza a configuração e recebe a ordem de abortar, se houver."""
        inicio = time.perf_counter()
        try:
            resposta = self.backend.heartbeat(self.status, self.latencia_ms)
        except (PermissionError, LookupError) as exc:
            self.abortar(str(exc))
            return
        except Exception:
            return  # sem contato: o ciclo suspende os lances depois de 6 s
        self.latencia_ms = int((time.perf_counter() - inicio) * 1000)
        with self._lock:
            self.ultimo_contato = self.relogio()
            if resposta.get("config"):
                self.config = resposta["config"]
        if resposta.get("abortar"):
            self.abortar(resposta.get("motivo") or "Kill-Switch acionado no painel web.")

    def em_contato(self) -> bool:
        return (self.relogio() - self.ultimo_contato) <= LIMITE_SEM_CONTATO_SEGUNDOS

    # ---- uma volta do loop --------------------------------------------------------
    def ciclo(self) -> str:
        """Uma volta do loop de disputa; devolve o status (OPERANDO, SUSPENSO_SEM_CONEXAO, ...)."""
        self._checar_parada()
        with self._lock:
            cfg = dict(self.config)
        if not self.em_contato():
            return self._status("SUSPENSO_SEM_CONEXAO", "Sem contato com o servidor há mais de 6 s: lances suspensos.")
        if cfg.get("kill_switch"):
            self.abortar("Kill-Switch acionado no painel web.")
            self._checar_parada()
        if str(cfg.get("fase", "")).upper() == "ENCERRADO":
            self.abortar("Disputa encerrada.")
            self._checar_parada()
        if cfg.get("status_robo") != "LIGADO":
            return self._status("AGUARDANDO_LIGAR", "Robô DESLIGADO no painel web: aguardando.")

        melhor = self.adaptador.ler_melhor_lance() or cfg.get("melhor_lance")
        lance = calcular_proximo_lance(melhor, cfg.get("meu_lance"), cfg.get("decremento_minimo"), cfg.get("preco_minimo"))
        if lance is None:
            return self._status("LIDERANDO" if cfg.get("meu_lance") else "OPERANDO", None)
        agora = self.relogio()
        if self.ultimo_lance_em is not None and agora - self.ultimo_lance_em < self.intervalo_minimo:
            return self._status("INTERVALO_MINIMO", None)

        # Última conferência antes do envio: parada, contato e piso
        self._checar_parada()
        if not self.em_contato():
            return self._status("SUSPENSO_SEM_CONEXAO", "Conexão perdida antes do envio: lance suspenso.")
        resposta = self.adaptador.enviar_lance(lance, centavos(cfg.get("preco_minimo")))
        self.ultimo_lance_em = self.relogio()
        momento_ms = int(time.time() * 1000)
        self.saida(f"[{time.strftime('%H:%M:%S')}] LANCE R$ {lance} -> {'ACEITO' if resposta.aceito else 'RECUSADO'} "
                   f"({resposta.latencia_ms} ms) {resposta.texto[:120]}")
        if resposta.aceito:
            self.lances_enviados += 1
            with self._lock:
                self.config["meu_lance"] = float(lance)
        try:
            retorno = self.backend.registrar_lance(lance, momento_ms, resposta.aceito, resposta.texto, resposta.latencia_ms)
            if retorno.get("abortar"):
                self.abortar("Kill-Switch acionado no painel web.")
        except StopLossViolado:
            raise
        except Exception as exc:
            self.saida(f"   ! Auditoria do lance não registrada no servidor: {exc}")
        return self._status("LANCE_ENVIADO", None)

    def _status(self, status: str, mensagem: Optional[str]) -> str:
        if status != self.status and mensagem:
            self.saida(f"[{time.strftime('%H:%M:%S')}] {mensagem}")
        self.status = status
        return status

    # ---- execução com threads -----------------------------------------------------
    def _laco_heartbeat(self):
        while not self.parar.is_set():
            self.pulso()
            self.parar.wait(HEARTBEAT_SEGUNDOS)

    def _laco_teclado(self):
        if os.name == "nt":
            import msvcrt
            while not self.parar.is_set():
                if msvcrt.kbhit() and msvcrt.getwch() in (" ", "q", "Q"):
                    self.abortar("Parada manual no terminal.")
                    return
                time.sleep(0.02)
        else:
            for linha in sys.stdin:
                if linha.strip().lower() in ("", "q"):
                    self.abortar("Parada manual no terminal.")
                    return

    def executar(self) -> int:
        threading.Thread(target=self._laco_heartbeat, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._laco_teclado, name="teclado", daemon=True).start()
        try:
            while True:
                self.ciclo()
                if self.parar.wait(TICK_SEGUNDOS):
                    self._checar_parada()
        except RoboAbortado as exc:
            self.saida(f"\n>>> ROBÔ PARADO: {exc}")
            return SAIDA_OK if "encerrada" in str(exc) else SAIDA_KILL
        except StopLossViolado as exc:
            self.parar.set()
            self.saida(f"\n>>> STOP LOSS: {exc}\n>>> Execução abortada antes de qualquer envio abaixo do piso.")
            return SAIDA_STOP_LOSS
        finally:
            self.parar.set()
            self.saida(f">>> Lances enviados nesta sessão: {self.lances_enviados}")


# ------------------------------------------------------------------------------
# Configuração local e tela inicial
# ------------------------------------------------------------------------------
def pasta_do_programa() -> str:
    if getattr(sys, "frozen", False):  # executável do PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def carregar_config(caminho: str) -> Dict[str, Any]:
    """
    Lê config.json (url_backend, item_id, token, portal, intervalo_minimo_segundos) e pergunta o que
    faltar. Sem token, faz login com e-mail e senha: a senha nunca é gravada, e a chave de sessão só
    vai para o arquivo se o operador pedir.
    """
    cfg: Dict[str, Any] = {}
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as arq:
            cfg = json.load(arq)
    gravar = dict(cfg)
    alterou = False
    if not cfg.get("url_backend"):
        cfg["url_backend"] = gravar["url_backend"] = input("URL do Licitações Brasil (ex.: https://seu-app.onrender.com): ").strip()
        alterou = True
    if not cfg.get("item_id"):
        cfg["item_id"] = gravar["item_id"] = input("ID do lote (como no cockpit, ex.: 12345:1): ").strip()
        alterou = True
    if not cfg.get("token"):
        email = input("E-mail do administrador: ").strip()
        senha = getpass.getpass("Senha (não é gravada): ")
        cfg["token"] = ClienteBackend.login(cfg["url_backend"], email, senha)
        if input("Guardar a chave de sessão em config.json? (s/N): ").strip().lower() == "s":
            gravar["token"] = cfg["token"]
            alterou = True
    if alterou:
        with open(caminho, "w", encoding="utf-8") as arq:
            json.dump(gravar, arq, ensure_ascii=False, indent=2)
    return cfg


def tela_inicial(cfg: Dict[str, Any], adaptador: AdaptadorPortal, intervalo: float) -> str:
    linha = "=" * 64
    modo = "SIMULAÇÃO (nada é enviado ao portal)" if not adaptador.envia_de_verdade else f"REAL - {adaptador.nome.upper()}"
    return "\n".join([
        linha,
        f"  ROBÔ DE LANCES - Licitações Brasil  v{VERSAO}",
        linha,
        f"  Edital / Pregão : {cfg.get('id_pregao')}   Item: {cfg.get('numero_item')}",
        f"  Objeto          : {str(cfg.get('objeto') or '')[:46]}",
        f"  Preço mínimo    : R$ {centavos(cfg.get('preco_minimo') or 0)}  (STOP LOSS)",
        f"  Decremento      : R$ {centavos(cfg.get('decremento_minimo') or 0)}",
        f"  Portal destino  : {cfg.get('portal')}   Modo: {modo}",
        f"  Robô no painel  : {cfg.get('status_robo')}   Intervalo mínimo entre lances: {intervalo:.0f} s",
        linha,
        "  ESPAÇO ou Q = parada imediata  |  Kill-Switch no cockpit web também para o robô",
        linha,
    ])


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        os.system("")  # habilita cores/ANSI no console do Windows
    args = argparse.ArgumentParser(description="Robô de Lances Desktop - Licitações Brasil")
    args.add_argument("--config", default=os.path.join(pasta_do_programa(), "config.json"))
    opcoes = args.parse_args(argv)
    try:
        cfg_local = carregar_config(opcoes.config)
        backend = ClienteBackend(cfg_local["url_backend"], cfg_local["token"], cfg_local["item_id"])
        robo_cfg = backend.config()
        adaptador = criar_adaptador(cfg_local.get("portal") or {}, robo_cfg.get("portal"))
        intervalo = float(cfg_local.get("intervalo_minimo_segundos", INTERVALO_MINIMO_PADRAO))
        robo = Robo(backend, adaptador, intervalo_minimo=intervalo)
        robo.handshake()
        print(tela_inicial(robo.config, adaptador, robo.intervalo_minimo))
        if centavos(robo.config.get("preco_minimo") or 0) <= 0:
            raise StopLossViolado("Preço mínimo (piso) não definido no painel: defina-o no cockpit antes de iniciar.")
        if input("Digite INICIAR para começar a disputa: ").strip().upper() != "INICIAR":
            print("Cancelado pelo operador.")
            return SAIDA_OK
        return robo.executar()
    except StopLossViolado as exc:
        print(f"STOP LOSS: {exc}")
        return SAIDA_STOP_LOSS
    except (KeyboardInterrupt, EOFError):
        print("\nInterrompido pelo operador.")
        return SAIDA_KILL
    except Exception as exc:
        print(f"ERRO: {exc}")
        return SAIDA_ERRO


if __name__ == "__main__":
    code = main()
    if getattr(sys, "frozen", False):
        input("\nPressione ENTER para fechar...")
    sys.exit(code)
