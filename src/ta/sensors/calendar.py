"""Agenda pelo Evolution Data Server, sobre DBus.

O GNOME faz o OAuth com um client já verificado e renova o token sozinho; o app
apenas lê o que o EDS já sincronizou (ADR 0004). Leitura local e instantânea, o
que é o que viabiliza usar agenda como condição de Rule sem pagar latência de
rede.

Exige `gi`, que só existe no Python do sistema (ADR 0005), e os typelibs
`gir1.2-ecal-2.0` e `gir1.2-edataserver-1.2`.

**UIDs de agenda nunca são fixados no código.** O EDS os regenera se a agenda for
recriada; a resolução é por nome de exibição + conta pai.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

log = logging.getLogger("ta.calendar")

# Agendas locais do EDS, que NÃO sincronizam com o Google. `system-calendar` se
# chama "Pessoal" e é a armadilha: gravar ali não aparece no celular.
LOCAIS = ("system-calendar", "birthdays")

ESCRITA = "Terminal Assistant"

# Provedores de e-mail pessoal. Uma conta em domínio próprio é tratada como
# trabalho, o que é a heurística certa aqui: quem tem domínio próprio no
# Calendar o tem por causa da empresa.
PROVEDORES_PESSOAIS = frozenset(
    {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
     "icloud.com", "me.com", "yahoo.com", "proton.me", "protonmail.com"}
)
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Quanto esperar pelo backend ficar online, por fonte. Pago uma vez por fonte,
# graças ao cache de clientes.
CONNECT_WAIT_S = 10


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    calendar: str
    source_uid: str

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class CalendarSource:
    uid: str
    name: str
    parent: str
    local: bool
    # E-mail da conta a que esta agenda pertence, quando dá para saber.
    #
    # Necessário porque `parent` é um **hash opaco** do GOA
    # ('d2c8054404442a86eb...'), não um nome legível: rotear por ele é
    # impossível. O sinal utilizável é que o Google sempre cria, na mesma conta,
    # uma agenda batizada com o e-mail dela — então o irmão que parece e-mail
    # identifica a conta inteira.
    account: str = ""

    @property
    def personal(self) -> bool:
        """Conta pessoal, pelo provedor do e-mail. Domínio próprio é trabalho."""
        dominio = self.account.rsplit("@", 1)[-1].lower() if "@" in self.account else ""
        return dominio in PROVEDORES_PESSOAIS


class Calendar:
    """Leitura e escrita nas agendas do EDS.

    Importa `gi` de forma tardia para que o resto do app funcione numa máquina
    sem os typelibs — a captura de notas não depende de agenda.
    """

    def __init__(self) -> None:
        self._registry = None
        self._erro: str | None = None
        # Conectar a uma fonte custa até CONNECT_WAIT_S, e uma agenda recém-criada
        # paga o timeout inteiro porque o EDS ainda não tem cache dela — medido:
        # 10s por agenda nova, 0,01s nas antigas. Sem cache, `ta today` levava 20s.
        self._clients: dict[str, object] = {}

    def _load(self):
        if self._registry is not None or self._erro is not None:
            return self._registry
        try:
            import gi

            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            gi.require_version("ICalGLib", "3.0")
            from gi.repository import EDataServer

            self._registry = EDataServer.SourceRegistry.new_sync(None)
        except (ImportError, ValueError) as e:
            # Falta a biblioteca ou o typelib: o conserto é `apt`.
            self._erro = (
                f"agenda indisponível: {e}. "
                "Rode `make check-gi` — provavelmente faltam os typelibs do apt."
            )
            log.warning("%s", self._erro)
        except Exception as e:  # noqa: BLE001
            # Os typelibs existem, mas não há barramento para falar com o
            # Evolution: `GLib.GError: Cannot autolaunch D-Bus without X11
            # $DISPLAY`. Acontece em servidor, container, SSH sem sessão gráfica
            # e no CI — e antes disto **estourava**, derrubando `ta doctor` e
            # `ta --help` justamente onde eles mais precisam responder.
            #
            # `Exception` largo de propósito: qualquer falha em alcançar o EDS
            # significa a mesma coisa para quem usa, que é "não tem agenda aqui",
            # e a alternativa é enumerar tipos de erro de uma pilha C.
            self._erro = (
                f"agenda indisponível: {e}. "
                "É esperado sem sessão gráfica — a agenda precisa do barramento "
                "do usuário. O resto do Terminal Assistant funciona sem ela."
            )
            log.warning("%s", self._erro)
        return self._registry

    @property
    def available(self) -> bool:
        return self._load() is not None

    @property
    def error(self) -> str | None:
        self._load()
        return self._erro

    # ── Fontes ──────────────────────────────────────────────────────────────
    def sources(self) -> list[CalendarSource]:
        reg = self._load()
        if reg is None:
            return []
        from gi.repository import EDataServer

        habilitadas = [
            s for s in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR) if s.get_enabled()
        ]
        # Primeiro descobre o e-mail de cada conta, pelo irmão batizado com ele.
        contas: dict[str, str] = {}
        for s in habilitadas:
            nome = s.get_display_name() or ""
            if RE_EMAIL.match(nome):
                contas[s.get_parent() or ""] = nome

        out = []
        for s in habilitadas:
            uid = s.get_uid()
            parent = s.get_parent() or ""
            out.append(
                CalendarSource(
                    uid=uid,
                    name=s.get_display_name(),
                    parent=parent,
                    local=uid in LOCAIS or parent.endswith("-stub"),
                    account=contas.get(parent, ""),
                )
            )
        return out

    def write_targets(self) -> list[CalendarSource]:
        """As agendas dedicadas de escrita — uma por conta (emenda do ADR 0007)."""
        return [s for s in self.sources() if s.name == ESCRITA and not s.local]

    # ── Clientes ────────────────────────────────────────────────────────────
    def _client(self, src):
        """Cliente conectado para uma fonte, memoizado.

        Sem isto, cada leitura reconecta a todas as fontes e paga o timeout das
        que ainda não estão em cache no EDS.
        """
        uid = src.get_uid()
        cached = self._clients.get(uid)
        if cached is not None:
            return cached
        from gi.repository import ECal

        client = ECal.Client.connect_sync(
            src, ECal.ClientSourceType.EVENTS, CONNECT_WAIT_S, None
        )
        self._clients[uid] = client
        return client

    def warm(self) -> int:
        """Conecta a todas as fontes, para as leituras seguintes serem instantâneas.

        **Em paralelo, e a razão é medida.** `connect_sync` recebe um
        `wait_for_connected_seconds` que, para fonte de nuvem, é consumido por
        inteiro: cada uma das 6 agendas do Google gastava exatos 10s e *então*
        conectava com sucesso. Em série isso dava 60s de boot, e o primeiro
        `ta today` depois de um restart estourava o timeout do CLI. Concorrente,
        o custo passa a ser o da fonte mais lenta, não a soma.

        O `dict` de clientes é escrito por várias threads, o que é seguro aqui:
        atribuição em `dict` é atômica, e duas threads que resolvam a mesma fonte
        gravariam clientes equivalentes.
        """
        reg = self._load()
        if reg is None:
            return 0
        from concurrent.futures import ThreadPoolExecutor

        from gi.repository import EDataServer

        fontes = [
            s for s in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR) if s.get_enabled()
        ]
        if not fontes:
            return 0

        def conectar(src) -> bool:
            try:
                self._client(src)
                return True
            except Exception:
                log.debug("não conectei em %s", src.get_display_name(), exc_info=True)
                return False

        with ThreadPoolExecutor(max_workers=len(fontes)) as pool:
            n = sum(pool.map(conectar, fontes))
        log.info("agenda aquecida: %d de %d fonte(s)", n, len(fontes))
        return n

    # ── Leitura ─────────────────────────────────────────────────────────────
    def events_between(self, inicio: datetime, fim: datetime) -> list[Event]:
        """Eventos no intervalo, com recorrências **expandidas**.

        Usa `generate_instances_sync`, não `get_object_list_as_comps_sync`: a
        segunda devolve o componente *mestre* de um evento recorrente, cujo
        DTSTART é o início da série. Uma "Daily" criada meses atrás apareceria
        com a data de criação, e várias ocorrências colapsariam numa. Foi
        exatamente o que apareceu no primeiro teste contra a agenda real.
        """
        reg = self._load()
        if reg is None:
            return []
        from gi.repository import EDataServer

        eventos: list[Event] = []
        for src in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR):
            if not src.get_enabled():
                continue
            try:
                client = self._client(src)

                def coletar(icomp, inst_start, inst_end, _data, _cancellable, src=src):
                    ev = _instancia(icomp, inst_start, inst_end, src)
                    if ev is not None:
                        eventos.append(ev)
                    return True   # True = continuar gerando

                client.generate_instances_sync(
                    int(inicio.timestamp()), int(fim.timestamp()), None, coletar, None
                )
            except Exception:
                # Uma agenda que falha não deve esconder as outras.
                log.debug("falha ao ler a agenda %s", src.get_display_name(), exc_info=True)

        eventos.sort(key=lambda e: (e.start, e.summary))
        return eventos

    def today(self, dia: date | None = None) -> list[Event]:
        dia = dia or date.today()
        return self.events_between(
            datetime.combine(dia, time.min), datetime.combine(dia, time.max)
        )

    def now(self, momento: datetime | None = None) -> Event | None:
        """O compromisso em curso, se houver. É o enriquecimento do ADR 0008."""
        momento = momento or datetime.now()
        for e in self.today(momento.date()):
            if not e.all_day and e.start <= momento <= e.end:
                return e
        return None

    # ── Escrita ─────────────────────────────────────────────────────────────
    def create_event(
        self, source_uid: str, summary: str, start: datetime, end: datetime
    ) -> str | None:
        """Cria um evento na agenda dada.

        Invariante do ADR 0007: **nunca adiciona convidados.** Não há parâmetro
        para isso, e não deve ganhar um.
        """
        reg = self._load()
        if reg is None:
            return None
        from gi.repository import ECal, ICalGLib

        src = reg.ref_source(source_uid)
        if src is None:
            log.error("agenda %s não existe", source_uid)
            return None

        client = self._client(src)
        comp = ICalGLib.Component.new_vevent()
        comp.set_summary(summary)
        comp.set_dtstart(_ical_time(start))
        comp.set_dtend(_ical_time(end))
        ok, uid = client.create_object_sync(comp, ECal.OperationFlags.NONE, None)
        return uid if ok else None


# ── Conversões ──────────────────────────────────────────────────────────────
def _ical(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _ical_time(dt: datetime):
    from gi.repository import ICalGLib

    t = ICalGLib.Time.new_null_time()
    t.set_date(dt.year, dt.month, dt.day)
    t.set_time(dt.hour, dt.minute, 0)
    t.set_is_date(False)
    return t


def _instancia(icomp, inst_start, inst_end, src) -> Event | None:
    """Uma ocorrência concreta. Os horários vêm da instância, não do mestre."""
    try:
        dtstart = icomp.get_dtstart()
        return Event(
            uid=icomp.get_uid() or "",
            summary=icomp.get_summary() or "(sem título)",
            start=_from_ical(inst_start),
            end=_from_ical(inst_end) if inst_end is not None else _from_ical(inst_start),
            all_day=bool(dtstart is not None and dtstart.is_date()),
            calendar=src.get_display_name(),
            source_uid=src.get_uid(),
        )
    except Exception:
        log.debug("componente de agenda ilegível", exc_info=True)
        return None


def _from_ical(t) -> datetime:
    return datetime(
        t.get_year(), t.get_month(), t.get_day(),
        max(0, t.get_hour()), max(0, t.get_minute()), max(0, t.get_second()),
    )
