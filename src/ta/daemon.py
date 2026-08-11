"""O daemon: um processo asyncio.

Serve a API que o CLI consome e o mural, e roda o motor de gatilhos — scheduler,
watcher de microfone e watcher de estado — no mesmo loop. Existe porque gatilho de
tempo e de estado do PC não sobrevivem num CLI que roda e morre.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import engine, priorities, store
from . import notes as notes_mod
from .actuators.home import Home, HomeError, StateWatcher
from .actuators.lighter import Lighter
from .actuators.notify import Notifier
from .config import Config, _comandavel, resolve_entity, resolve_targets
from .db import connect
from .llm import LLM, LLMUnavailable
from .scheduler import Scheduler, atraso_de, texto_de_atraso
from .sensors.calendar import Calendar
from .sensors.mic import MicWatcher

log = logging.getLogger("ta")

# `ta on quarto` sem número acende a luz no máximo. Um padrão menor faria o
# comando mais curto ser o mais fraco, o que não é o que ninguém quer digitando
# "liga a luz".
BRILHO_PADRAO = 100

BOARD_HTML = Path(__file__).parent / "web" / "board.html"
RULES_DIR = Path(__file__).resolve().parents[2] / "rules"


def _note_json(n: store.Note, *, today: date | None = None) -> dict:
    """Serializa uma Note para o cliente.

    `horizon` vai junto e é derivado aqui, no servidor: ele depende do relógio, e
    uma segunda definição de "próximos 7 dias" vivendo no JS do mural seria uma
    definição que nenhum teste compara com esta (ADR 0010). Quem serializa uma
    lista passa `today` calculado UMA vez, para um payload longo não atravessar a
    meia-noite no meio dele.
    """
    return {
        "id": n.id,
        "text": n.text,
        "created_at": n.created_at,
        "due": n.due,
        "horizon": store.horizon(n.due, today=today),
        "remind_at": n.remind_at,
        "done": n.is_done,
        # O instante em que entrou em `done`. Distinto de `status`: estado e
        # carimbo de tempo são coisas diferentes.
        "done_at": n.done_at,
        "priority": n.priority,
        "group": n.group_name,
        "sort_key": n.sort_key,
        "pos": [n.pos_x, n.pos_y] if n.pos_x is not None else None,
        "color": n.color,
        "pinned_by_user": n.pinned_by_user,
        "status": n.status,
        "terminal": n.is_terminal,
        "tags": n.tags,
        "deleted_at": n.deleted_at,
        # Papéis derivados, explicitados para o cliente não recalcular a regra.
        "roles": {"task": n.is_task, "reminder": n.is_reminder},
    }


def _event_json(e) -> dict:
    return {
        "summary": e.summary,
        "start": e.start.isoformat(timespec="minutes"),
        "end": e.end.isoformat(timespec="minutes"),
        "all_day": e.all_day,
        "calendar": e.calendar,
    }


# ── Contexto do motor ───────────────────────────────────────────────────────
class CalendarAdapter:
    """A fachada que as Rules recebem em `ctx.calendar`.

    Fina de propósito: uma Rule não deve saber que existe EDS, DBus ou `gi`.
    """

    def __init__(self, cal: Calendar) -> None:
        self._cal = cal

    async def agora(self) -> dict | None:
        ev = await asyncio.to_thread(self._cal.now)
        return _event_json(ev) if ev else None

    async def hoje(self) -> list[dict]:
        evs = await asyncio.to_thread(self._cal.today)
        return [_event_json(e) for e in evs]


def _make_context(app: Starlette, trigger: engine.Trigger, **extra) -> engine.Context:
    return engine.Context(
        trigger=trigger,
        now=datetime.now(),
        home=app.state.home,
        lighter=app.state.lighter,
        notify=app.state.notify,
        calendar=app.state.cal_adapter,
        **extra,
    )


# ── Rotas ───────────────────────────────────────────────────────────────────
async def health(request: Request) -> JSONResponse:
    """Diagnóstico. Reporta *presença* de segredo, nunca o valor.

    Existe porque `systemctl show` não expõe o que veio de `EnvironmentFile`, e
    "o serviço está lendo o .env?" é a primeira pergunta quando algo do HA ou do
    Gemini falha.
    """
    app = request.app
    cfg: Config = app.state.config
    return JSONResponse(
        {
            "ok": True,
            "ha": {"url": cfg.ha_url, "token_configured": bool(cfg.ha_token)},
            "gemini": {
                "key_configured": bool(cfg.gemini_api_key),
                "model": app.state.llm.model,
            },
            "calendar": {"available": app.state.calendar.available},
            "mic": {"available": app.state.mic.available, "active": app.state.mic.active},
            "lighter": {"available": app.state.lighter.available},
            "notify": {"available": app.state.notify.available},
            "rules": {
                "loaded": [r.name for r in app.state.rules],
                "errors": [f for f, _ in app.state.rule_errors],
            },
        }
    )


async def notes_create(request: Request) -> JSONResponse:
    body = await request.json()
    raw = (body.get("text") or "").strip()
    if not raw:
        return JSONResponse({"error": "texto vazio"}, status_code=400)
    note = store.add_note(request.app.state.conn, raw)
    # A revisão sai em background e o 201 volta agora: captura não espera rede
    # (ADR 0003). O que ela mudar aparece no mural no próximo reload.
    _agendar_revisao(request.app, note)
    return JSONResponse(_note_json(note), status_code=201)


# Piso de confiança para agir sozinho. Abaixo disto a revisão não faz nada: uma
# correção errada é pior que nenhuma, e evento fantasma na agenda de trabalho é
# visível para colegas (emenda do ADR 0007).
CONFIANCA_MINIMA = 0.7

# Teto de tentativas por Note. Rede caída é temporário e merece retentativa;
# resposta que nunca valida não merece, e sem teto a fila viraria laço.
MAX_TENTATIVAS_REVISAO = 5

# Revisões simultâneas. "Revisar tudo" com 40 notas abriria 40 chamadas de uma
# vez e tomaria rate limit; em fila de 4 leva alguns segundos mais e não falha.
REVISOES_SIMULTANEAS = 4


def _agendar_revisao(
    app: Starlette, note: store.Note | None = None, *, limite: int = 10
) -> None:
    """Dispara a revisão da Note nova e drena a fila de atrasadas.

    A fila existe porque a revisão dispara uma vez, na captura: quem foi
    capturado sem rede nunca seria revisado. Cada captura nova com rede paga
    também pelas que ficaram para trás — é o gatilho mais natural, porque
    significa que você está usando o app e provavelmente tem rede agora.
    """
    if not app.state.auto_review or not app.state.llm.configured:
        return

    ids = [note.id] if note is not None else []
    # `pending_review` já exclui as que estouraram o teto de tentativas, e a Note
    # que acabou de nascer também está pendente — daí o filtro.
    ids += [i for i in store.pending_review(app.state.conn, limit=limite) if i not in ids]

    # Sem este filtro, quatro capturas em sequência reenfileiravam as mesmas
    # pendentes: a #5 foi revisada 4 vezes, e duas passadas na mesma nota deram
    # prazos DIFERENTES. `reviewed_at` só é gravado no fim, então a fila não
    # protege contra concorrência — este conjunto é que protege.
    ids = [i for i in ids if i not in app.state.em_revisao]

    for note_id in ids:
        app.state.em_revisao.add(note_id)
        tarefa = asyncio.create_task(_revisar_captura(app, note_id), name=f"review-{note_id}")
        # Guardar a referência: task sem dono pode ser coletada antes de terminar.
        app.state.revisoes.add(tarefa)
        tarefa.add_done_callback(app.state.revisoes.discard)


async def _revisar_captura(app: Starlette, note_id: int) -> None:
    """Corrige o que o regex não podia saber, e cria o evento se for o caso.

    Roda solta: qualquer falha aqui é registrada e morre nela mesma. A Note já
    está gravada, e o pior caso desta função é não acontecer.
    """
    try:
        async with app.state.revisao_sem:
            await _revisar_uma(app, note_id)
    finally:
        # Sai do conjunto de "em voo" aconteça o que acontecer, senão uma falha
        # trancaria a Note fora de qualquer retentativa futura.
        app.state.em_revisao.discard(note_id)


async def _revisar_uma(app: Starlette, note_id: int) -> None:
    try:
        note = store.get_note(app.state.conn, note_id)
    except KeyError:
        return  # apagada antes da revisão chegar

    # Conta a tentativa ANTES de tentar: se o processo morrer no meio, a Note não
    # fica tentando para sempre na próxima subida.
    tentativas = store.count_review_attempt(app.state.conn, note_id)

    try:
        # Exception larga de propósito: esta função é opcional por desenho, e
        # nada que ela faça vale derrubar o daemon ou perder a Note.
        alvos = await asyncio.to_thread(app.state.calendar.write_targets)
        r = await app.state.llm.review_capture(
            note.text,
            due=note.due,
            remind_at=note.remind_at,
            priorities=priorities.current(app.state.conn) or "",
            # Só o domínio, não o endereço: é o que decide o roteamento, e
            # mandar o e-mail inteiro para fora seria dado a mais pelo mesmo
            # resultado.
            contas=", ".join(
                f"{'pessoal' if a.personal else 'trabalho'}: "
                f"{a.account.rsplit('@', 1)[-1] if '@' in a.account else '?'}"
                for a in alvos
            ),
        )
    except Exception as e:
        # Não marca como revisada: fica na fila para a próxima captura com rede.
        if tentativas >= MAX_TENTATIVAS_REVISAO:
            store.mark_reviewed(app.state.conn, note_id)
            log.warning(
                "revisão de #%s desistiu após %d tentativas: %s", note_id, tentativas, e
            )
        else:
            log.info(
                "revisão de #%s falhou (tentativa %d/%d), fica na fila: %s",
                note_id, tentativas, MAX_TENTATIVAS_REVISAO, e,
            )
        return

    # Daqui em diante houve resposta do modelo: sai da fila mesmo que nada mude.
    store.mark_reviewed(app.state.conn, note_id)

    if r.confidence < CONFIANCA_MINIMA:
        log.info("revisão de #%s ignorada: confiança %.2f", note_id, r.confidence)
        return

    avisos: list[str] = []

    novo_due = _iso_data(r.due)
    novo_remind = _iso_datahora(r.remind_at)
    if (novo_due, novo_remind) != (
        date.fromisoformat(note.due) if note.due else None,
        datetime.fromisoformat(note.remind_at) if note.remind_at else None,
    ):
        store.set_schedule(app.state.conn, note_id, due=novo_due, remind_at=novo_remind)
        if note.due and novo_due is None:
            avisos.append("prazo removido")
        elif novo_due:
            avisos.append(f"prazo {novo_due.isoformat()}")

    # Prioridade e tags só entram se **você** não as escreveu. `by_user` é o que
    # separa "digitei `!alta`" de "a revisão pôs alta na passada anterior": o
    # primeiro é intocável, o segundo é revisável.
    tipo = r.intent if r.intent in notes_mod.TIPOS else "anotacao"

    if not note.priority_by_user:
        # Anotação não tem prioridade, por definição: registro e ideia solta não
        # são cobráveis, e pedir urgência delas só suja o mural. Se havia
        # prioridade posta por máquina, ela sai.
        nova = r.priority if (tipo != "anotacao" and r.priority in notes_mod.PRIORITIES) else None
        if nova != note.priority:
            store.set_priority(app.state.conn, note_id, nova)
            avisos.append(f"prioridade {nova}" if nova else "prioridade removida")

    # Três eixos, todos como tag para poderem ser filtrados e vistos no post-it:
    # área (trabalho/pessoal), tipo (tarefa/compromisso/anotacao) e até dois temas.
    #
    # Área e tipo entram SEMPRE, inclusive quando você escreveu tags à mão. Eles
    # são estrutura, não tema: sem eles a nota desaparece dos filtros de área e
    # de tipo, e "invisível no filtro" é pior que "mal classificada". A primeira
    # versão disto tratava suas tags como tudo-ou-nada e produziu exatamente esse
    # buraco numa nota real.
    area = "trabalho" if r.account == "trabalho" else "pessoal"
    eixos = [area, tipo]

    if note.tags_by_user:
        # Seus temas ficam intocados; só o que falta de eixo é acrescentado.
        novas = list(dict.fromkeys([*note.tags, *eixos]))
    else:
        temas = [
            x for x in dict.fromkeys(r.tags) if x in notes_mod.TAGS_SUGERIDAS and x not in eixos
        ][:2]
        novas = [*eixos, *temas]

    if sorted(novas) != sorted(note.tags):
        store.set_tags(app.state.conn, note_id, novas)
        avisos.append("tags " + " ".join(f"#{x}" for x in sorted(novas)))

    if r.is_event and r.start:
        uid = await _criar_evento_automatico(app, note_id, r, alvos)
        if uid:
            avisos.append(f"evento criado: {r.title}")

    if avisos:
        # Autonomia invisível é pior que nenhuma: se o app mexeu na sua nota ou
        # escreveu na sua agenda, você fica sabendo na hora.
        await app.state.notify.send(
            "Nota revisada",
            f"#{note_id}: {', '.join(avisos)}" + (f" — {r.reason}" if r.reason else ""),
            urgency="low",
        )
        log.info("revisão de #%s: %s", note_id, "; ".join(avisos))


async def _criar_evento_automatico(app: Starlette, note_id: int, r, alvos) -> str | None:
    """Cria o evento na agenda dedicada da conta certa.

    O usuário decidiu abrir mão da confirmação (emenda do ADR 0007). As outras
    duas guardas continuam: **nunca convidados**, porque isso dispararia e-mail
    para gente real, e **só na agenda "Terminal Assistant"**, que é o que mantém
    tudo apagável de uma vez.
    """
    # Esta Note já tem evento? Então não cria outro.
    #
    # `calendar_links` tem PRIMARY KEY em `note_id` e o INSERT era OR REPLACE, o
    # que trocava o vínculo e deixava o evento anterior **órfão na agenda**. Uma
    # nota reetiquetada três vezes gerou três compromissos idênticos, e o banco
    # só sabia do último. Duplicar compromisso é pior que não atualizar título.
    já = app.state.conn.execute(
        "SELECT uid FROM calendar_links WHERE note_id = ?", (note_id,)
    ).fetchone()
    if já is not None:
        log.info("nota #%s já tem evento (%s): não crio outro", note_id, já["uid"][:16])
        return None

    if not alvos:
        log.warning("nenhuma agenda 'Terminal Assistant': evento de #%s não criado", note_id)
        return None

    # Rotear por `parent` não funcionava: é um hash opaco do GOA, então a
    # comparação era sempre falsa e TODO evento caía na mesma agenda,
    # independentemente de o LLM ter dito 'trabalho' ou 'pessoal'. Erro
    # silencioso: o evento aparecia, só na conta errada.
    quer_pessoal = r.account != "trabalho"
    escolhida = next((a for a in alvos if a.personal == quer_pessoal), alvos[0])
    try:
        inicio = datetime.fromisoformat(r.start)
        fim = datetime.fromisoformat(r.end) if r.end else inicio + timedelta(hours=1)
    except ValueError:
        log.info("revisão de #%s devolveu data inválida: %r", note_id, r.start)
        return None

    uid = await asyncio.to_thread(
        app.state.calendar.create_event, escolhida.uid, r.title or "(sem título)", inicio, fim
    )
    if uid:
        app.state.conn.execute(
            "INSERT OR REPLACE INTO calendar_links (note_id, uid, source_uid, created_at)"
            " VALUES (?, ?, ?, ?)",
            (note_id, uid, escolhida.uid, datetime.now().isoformat(timespec="seconds")),
        )
    return uid


def _iso_data(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _iso_datahora(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


async def review_all(request: Request) -> JSONResponse:
    """Devolve todas as Notes abertas à fila de revisão e começa a drenar.

    Existe porque as Notes anteriores à revisão nunca passaram por ela, e porque
    editar as Priorities muda o que a revisão decidiria — reetiquetar tudo é o
    jeito de aplicar o contexto novo ao que já estava escrito.
    """
    app = request.app
    if not app.state.llm.configured:
        return JSONResponse({"error": "GEMINI_API_KEY não configurada"}, status_code=400)
    if not app.state.auto_review:
        return JSONResponse(
            {"error": "revisão desligada (TA_AUTO_REVIEW=0)"}, status_code=400
        )

    n = store.queue_all_for_review(app.state.conn)
    _agendar_revisao(app, limite=200)
    return JSONResponse({"queued": n, "running": len(app.state.em_revisao)})


async def review_status(request: Request) -> JSONResponse:
    """Quantas faltam. O mural usa isto para mostrar progresso."""
    conn = request.app.state.conn
    return JSONResponse(
        {
            "pending": conn.execute(
                "SELECT COUNT(*) FROM notes WHERE reviewed_at IS NULL"
            ).fetchone()[0],
            "running": len(request.app.state.em_revisao),
        }
    )


async def notes_list(request: Request) -> JSONResponse:
    include_done = request.query_params.get("done") == "1"
    # `deleted=1` devolve SÓ as apagadas: é a lixeira, não um "inclui também".
    deleted = request.query_params.get("deleted") == "1"
    notes = store.list_notes(
        request.app.state.conn, include_done=include_done, deleted=deleted
    )
    # A ordem de exibição sai daqui, não do cliente: as três visões do mural, o
    # `ta list` e qualquer outro consumidor recebem a mesma ordem sem cada um
    # reimplementá-la (ADR 0010).
    hoje = date.today()
    notes = store.by_urgency(notes, today=hoje)
    return JSONResponse({"notes": [_note_json(n, today=hoje) for n in notes]})


async def notes_delete(request: Request) -> JSONResponse:
    """Apaga de forma reversível. O evento na agenda, se houver, fica.

    Apagar a anotação sobre um compromisso não desmarca o compromisso — quem
    apaga um post-it não está cancelando a consulta no dentista.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    store.soft_delete(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_purge(request: Request) -> JSONResponse:
    """Apaga em definitivo uma nota **que já está na lixeira**.

    O 409 quando ela não está é deliberado: recusar é melhor que apagar de
    surpresa algo que o usuário achava seguro.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        nota = store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    if not nota.is_deleted:
        return JSONResponse(
            {"error": f"nota {note_id} não está na lixeira. Apague primeiro."},
            status_code=409,
        )
    store.purge(conn, note_id)
    return JSONResponse({"purged": 1, "id": note_id})


async def trash_purge(request: Request) -> JSONResponse:
    """Esvazia a lixeira. Sem volta, e por isso exige `confirmed`."""
    body = await request.json() if await request.body() else {}
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": "confirmação explícita é obrigatória: isto não tem volta"},
            status_code=400,
        )
    n = store.purge_all(request.app.state.conn)
    return JSONResponse({"purged": n})


async def notes_restore(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    store.restore(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_move(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    body = await request.json()
    store.move_note(
        request.app.state.conn,
        note_id,
        sort_key=body.get("sort_key"),
        pos_x=body.get("pos_x"),
        pos_y=body.get("pos_y"),
        group_name=body.get("group"),
        color=body.get("color"),
    )
    return JSONResponse(_note_json(store.get_note(request.app.state.conn, note_id)))


async def notes_done(request: Request) -> JSONResponse:
    """Alterna conclusão. Aceita `{"done": false}` para desmarcar."""
    note_id = int(request.path_params["note_id"])
    done = True
    if await request.body():
        done = bool((await request.json()).get("done", True))
    conn = request.app.state.conn
    store.mark_done(conn, note_id) if done else store.mark_undone(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_status(request: Request) -> JSONResponse:
    """Muda o estado da Note. É o que o arrastar entre colunas do kanban chama."""
    note_id = int(request.path_params["note_id"])
    status = (await request.json()).get("status", "")
    conn = request.app.state.conn
    try:
        store.set_status(conn, note_id, status)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def export(request: Request) -> PlainTextResponse:
    return PlainTextResponse(store.export_markdown(request.app.state.conn))


async def board(request: Request) -> HTMLResponse:
    """Serve o mural, sempre relido do disco e nunca cacheado.

    `no-store` não é zelo excessivo: o arquivo muda junto com o código, e o
    Chrome cacheia HTML sem header por heurística própria. Sem isto, uma edição
    no mural só aparecia depois de recarga forçada — e eu perdi tempo achando que
    o CSS estava errado quando era só cache.
    """
    return HTMLResponse(
        BOARD_HTML.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


async def today(request: Request) -> JSONResponse:
    """O Digest: compromissos da agenda + Notes cobráveis.

    Determinístico e instantâneo — o LLM nunca entra aqui (ADR 0003). A prosa
    gerada é enfeite opcional, pedida à parte.
    """
    app = request.app
    param = request.query_params.get("date")
    dia = date.fromisoformat(param) if param else None
    # O aquecimento leva ~30s no boot, e ler a agenda antes de ele terminar
    # bloqueia pelo mesmo tempo. Esperar aqui e DIZER que esperou é melhor que o
    # CLI estourar o timeout e o usuário achar que o daemon morreu.
    aquecendo = False
    warm = getattr(app.state, "warm_task", None)
    if warm is not None and not warm.done():
        aquecendo = True
        with contextlib.suppress(Exception):
            await warm

    eventos = await asyncio.to_thread(app.state.calendar.today, dia)
    # `dia` e não `date.today()`: com `--date`, a faixa tem de ser contada contra
    # o dia pedido, senão tudo o que ele devolve vira `vencida`. Aqui só aparecem
    # `vencida` e `hoje`, porque `due_today` filtra `due <= dia`.
    hoje = dia or date.today()
    tarefas = store.by_urgency(store.due_today(app.state.conn, today=dia), today=hoje)
    # Clima entra no Digest porque foi pedido, e degrada a None em silêncio: o
    # Digest não deve falhar porque o HA está fora do ar.
    clima = None
    with contextlib.suppress(HomeError):
        clima = (await app.state.home.sensors())["weather"]
    return JSONResponse(
        {
            "date": hoje.isoformat(),
            "weather": clima,
            "calendar_available": app.state.calendar.available,
            "calendar_error": app.state.calendar.error,
            "calendar_warming": aquecendo,
            "events": [_event_json(e) for e in eventos],
            "tasks": [_note_json(n, today=hoje) for n in tarefas],
        }
    )


async def home_light(request: Request) -> JSONResponse:
    """Liga um ou vários alvos. O termo pode ser entity_id, grupo ou ambiente."""
    body = await request.json()
    termo = (body.get("entity") or "").strip()
    if not termo:
        return JSONResponse({"error": "falta o entity"}, status_code=400)
    home = request.app.state.home
    try:
        alvos = resolve_targets(termo, await home.entities("light.", "switch."))
        if not alvos:
            return JSONResponse(
                {"error": f"nada casou com {termo!r}. Veja `ta entities`."}, status_code=404
            )
        # Sem brilho explícito, ligar uma luz quer dizer ligar por inteiro. O
        # domínio `switch` ignora o valor (veja `Home.switch_on`), então o padrão
        # não muda nada para a tomada.
        brilho = int(body["brightness"]) if "brightness" in body else BRILHO_PADRAO
        # 0 apaga; qualquer outro valor (ou nenhum) liga.
        esperado = "off" if brilho == 0 else "on"
        resultados = []
        for entity in alvos:
            await home.switch_on(entity, brilho)
            estado, confirmado = await home.confirm(entity, esperado)
            resultados.append(
                {"entity_id": entity, "state": estado, "confirmed": confirmado}
            )
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"matched": termo, "results": resultados})


async def home_off(request: Request) -> JSONResponse:
    body = await request.json() if await request.body() else {}
    alvo = body.get("entity")
    home = request.app.state.home
    try:
        if alvo:
            entidades = resolve_targets(alvo, await home.entities("light.", "switch."))
            if not entidades:
                return JSONResponse(
                    {"error": f"nada casou com {alvo!r}. Veja `ta entities`."}, status_code=404
                )
        else:
            # Sem alvo, apaga tudo que está aceso. Nunca lista fixa no código: o
            # inventário é do HA (ADR 0001).
            entidades = [
                e["entity_id"]
                for e in await home.entities("light.", "switch.")
                if e["state"] == "on" and _comandavel(e)
            ]
        resultados = []
        for entity in entidades:
            await home.turn_off(entity)
            estado, confirmado = await home.confirm(entity, "off")
            resultados.append({"entity_id": entity, "state": estado, "confirmed": confirmado})
        await request.app.state.lighter.enable(False)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"turned_off": [r["entity_id"] for r in resultados], "results": resultados})


async def sensors_route(request: Request) -> JSONResponse:
    """Clima, roteador e consumo da tomada. Curado — ver Home.sensors()."""
    try:
        return JSONResponse(await request.app.state.home.sensors())
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def home_entities(request: Request) -> JSONResponse:
    try:
        ents = await request.app.state.home.entities("light.", "switch.", "media_player.")
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(
        {"entities": [{"entity_id": e["entity_id"], "state": e["state"]} for e in ents]}
    )


async def media(request: Request) -> JSONResponse:
    """Mídia nos Echo. Zero código de Alexa: são `media_player` do HA (ADR 0009)."""
    body = await request.json()
    cfg = request.app.state.config
    pedido = (body.get("entity") or "").strip()
    # Sem alvo explícito, o alvo é o Echo configurado. Uma casa com um Echo não
    # deveria ter que repetir o entity_id em todo comando.
    entity = resolve_entity(pedido) if pedido else next(iter(cfg.echo_entities), "")
    if not entity:
        return JSONResponse(
            {
                "error": "nenhum Echo configurado. Defina TA_ECHOS no .env com o "
                "entity_id do media_player, ou passe o alvo no comando."
            },
            status_code=400,
        )
    if not entity.startswith("media_player."):
        # Sem isto o HA devolve um 400 sem corpo útil e o erro chega ilegível.
        return JSONResponse(
            {"error": f"{entity!r} não é um media_player. Veja `ta entities`."},
            status_code=400,
        )
    home = request.app.state.home
    try:
        if "volume" in body:
            await home.volume(entity, int(body["volume"]))
        elif "play" in body:
            await home.play_media(entity, str(body["play"]))
        elif "action" in body:
            await home.media(entity, str(body["action"]))
        elif "announce" in body:
            await home.announce(entity, str(body["announce"]))
        else:
            return JSONResponse({"error": "nada a fazer"}, status_code=400)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "entity_id": entity})


async def lighter_route(request: Request) -> JSONResponse:
    body = await request.json() if await request.body() else {}
    lg = request.app.state.lighter
    if not lg.available:
        return JSONResponse({"error": "Lighter não está instalada"}, status_code=502)
    if "profile" in body:
        ok = await lg.apply_profile(str(body["profile"]))
    elif body.get("toggle"):
        ok = await lg.toggle()
    else:
        ok = await lg.enable(bool(body.get("on", True)))
    return JSONResponse({"ok": ok, "enabled": await lg.get("enabled")})


async def organize(request: Request) -> JSONResponse:
    """Agrupa e ordena com o LLM, e GRAVA o resultado.

    Abrir o mural depois nunca chama o modelo, e o que foi arrastado à mão não é
    desfeito (ADR 0003).

    Desde o ADR 0010 o prazo não é mais assunto do modelo: a faixa de horizonte é
    derivada do relógio e vem na frente de tudo na exibição. O que o `organize`
    grava em `sort_key` refina a ordem **dentro** da faixa.
    """
    app = request.app
    conn = app.state.conn
    # De propósito na ordem GRAVADA, sem `by_urgency`: é o que o modelo tem de
    # ver para refinar, e é a ordem que ele vai reescrever.
    notes = store.list_notes(conn)
    if not notes:
        return JSONResponse({"placed": 0, "groups": []})
    hoje = date.today()
    try:
        res = await app.state.llm.organize(
            [_note_json(n, today=hoje) for n in notes], priorities.current(conn) or ""
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    ordem = {g: i for i, g in enumerate(res.groups_in_order)}
    conhecidos = {n.id: n for n in notes}
    aplicados = 0
    for p in res.placements:
        n = conhecidos.get(p.id)
        if n is None or n.pinned_by_user:
            continue   # a mão do usuário vence a do modelo
        conn.execute(
            "UPDATE notes SET group_name = ?, sort_key = ? WHERE id = ?",
            (p.group, ordem.get(p.group, 99) * 1000 + p.rank, p.id),
        )
        aplicados += 1
    return JSONResponse(
        {
            "placed": aplicados,
            "skipped_pinned": sum(1 for n in notes if n.pinned_by_user),
            "groups": res.groups_in_order,
            "model": app.state.llm.model,
        }
    )


async def detect_event(request: Request) -> JSONResponse:
    """Propõe um evento a partir de uma nota. NÃO cria nada (ADR 0007)."""
    app = request.app
    body = await request.json()
    note_id = body.get("note_id")
    texto = body.get("text")
    if note_id is not None:
        texto = store.get_note(app.state.conn, int(note_id)).text
    if not texto:
        return JSONResponse({"error": "falta text ou note_id"}, status_code=400)
    try:
        cand = await app.state.llm.detect_event(texto)
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    alvos = {
        ("trabalho" if s.parent.startswith("b1d1") else "pessoal"): s.uid
        for s in app.state.calendar.write_targets()
    }
    return JSONResponse(
        {
            "candidate": cand.model_dump(),
            "targets": alvos,
            # Nada foi criado. O cliente confirma via POST /calendar/event.
            "created": False,
        }
    )


async def create_event(request: Request) -> JSONResponse:
    """Grava o evento confirmado. Nunca com convidados (ADR 0007)."""
    app = request.app
    body = await request.json()
    for campo in ("source_uid", "title", "start", "end"):
        if not body.get(campo):
            return JSONResponse({"error": f"falta {campo}"}, status_code=400)
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": "confirmação explícita é obrigatória (ADR 0007)"}, status_code=400
        )
    uid = await asyncio.to_thread(
        app.state.calendar.create_event,
        body["source_uid"],
        body["title"],
        datetime.fromisoformat(body["start"]),
        datetime.fromisoformat(body["end"]),
    )
    if uid is None:
        return JSONResponse({"error": "não consegui criar o evento"}, status_code=502)
    if body.get("note_id"):
        app.state.conn.execute(
            "INSERT OR REPLACE INTO calendar_links (note_id, uid, source_uid, created_at)"
            " VALUES (?, ?, ?, ?)",
            (int(body["note_id"]), uid, body["source_uid"],
             datetime.now().isoformat(timespec="seconds")),
        )
    return JSONResponse({"created": True, "uid": uid})


async def priorities_route(request: Request) -> JSONResponse:
    app = request.app
    conn = app.state.conn
    if request.method == "GET":
        return JSONResponse(
            {"content": priorities.current(conn), "questions": priorities.PERGUNTAS}
        )
    body = await request.json()
    if "answers" in body:
        conteudo = priorities.from_answers(body["answers"])
        priorities.save(conn, conteudo)
        return JSONResponse({"content": conteudo})
    if "instruction" in body:
        try:
            conteudo = await priorities.rewrite(conn, app.state.llm, body["instruction"])
        except LLMUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return JSONResponse({"content": conteudo})
    if "content" in body:
        priorities.save(conn, body["content"])
        return JSONResponse({"content": body["content"]})
    return JSONResponse({"error": "nada a fazer"}, status_code=400)


async def digest_prose(request: Request) -> JSONResponse:
    """A prosa do dia. Enfeite opcional — a listagem é o padrão (ADR 0003)."""
    app = request.app
    eventos = await asyncio.to_thread(app.state.calendar.today)
    hoje = date.today()
    tarefas = store.by_urgency(store.due_today(app.state.conn), today=hoje)
    try:
        texto = await app.state.llm.digest_prose(
            [_event_json(e) for e in eventos], [_note_json(n, today=hoje) for n in tarefas]
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"text": texto})


async def models_route(request: Request) -> JSONResponse:
    llm = request.app.state.llm
    try:
        return JSONResponse({"current": llm.model, "available": await llm.list_models()})
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)


async def rules_route(request: Request) -> JSONResponse:
    app = request.app
    return JSONResponse(
        {
            "dir": str(RULES_DIR),
            "rules": [
                {"name": r.name, "on": [str(t) for t in r.on], "source": r.source}
                for r in app.state.rules
            ],
            "errors": [{"file": f, "traceback": tb} for f, tb in app.state.rule_errors],
        }
    )


# ── Reação aos gatilhos ─────────────────────────────────────────────────────
async def _fire_reminders(app: Starlette, agora: datetime) -> None:
    """Reminders vencidos: notifica e marca. Fecha o loop tempo → ação.

    A notificação de desktop é o caminho obrigatório; o anúncio no Echo é
    adicional, e sua ausência ou falha nunca impede o aviso (ADR 0009).
    """
    conn = app.state.conn
    for note in store.pending_reminders(conn, now=agora):
        atraso = texto_de_atraso(atraso_de(note.remind_at, agora))
        await app.state.notify.send("Lembrete", f"{note.text}{atraso}", urgency="critical")

        for echo in app.state.config.echo_entities:
            with contextlib.suppress(HomeError):
                await app.state.home.announce(echo, f"Lembrete: {note.text}")

        store.mark_fired(conn, note.id, now=agora)
        log.info("reminder #%s disparado%s", note.id, atraso)
        await engine.dispatch(
            app.state.rules,
            _make_context(app, engine.Trigger("reminder", note.id), note=_note_json(note)),
        )


def _wire_engine(app: Starlette) -> None:
    async def on_mic(ativo: bool, apps: list[str]) -> None:
        await engine.dispatch(
            app.state.rules,
            _make_context(app, engine.Trigger("mic", ativo), extra={"apps": apps}),
        )

    async def on_time(minuto: str, agora: datetime) -> None:
        await engine.dispatch(app.state.rules, _make_context(app, engine.Trigger("time", minuto)))

    async def on_state(entity_id: str, velho: str, novo: str) -> None:
        await engine.dispatch(
            app.state.rules,
            _make_context(
                app, engine.Trigger("state", entity_id), extra={"from": velho, "to": novo}
            ),
        )

    app.state.mic = MicWatcher(on_mic)
    app.state.scheduler = Scheduler(on_time, lambda agora: _fire_reminders(app, agora))
    app.state.state_watcher = StateWatcher(app.state.home, on_state)


# ── App ─────────────────────────────────────────────────────────────────────
def create_app(
    config: Config | None = None,
    *,
    db_path=None,
    rules_dir=None,
    calendar=None,
    lighter=None,
    background: bool = True,
) -> Starlette:
    """Monta o app.

    `calendar`, `lighter` e `background` existem para injeção: sem eles, cada teste que
    sobe o app conectaria ao Evolution Data Server de verdade e pagaria o
    aquecimento das fontes. Teste não deve tocar o ambiente do usuário.
    """
    cfg = config or Config.from_env()
    rules_path = Path(rules_dir) if rules_dir else RULES_DIR

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.config = cfg
        app.state.auto_review = cfg.auto_review
        # Referências fortes das revisões em voo: task sem dono pode ser coletada
        # pelo GC antes de terminar.
        app.state.revisoes = set()
        # Ids com revisão em voo. Impede que duas capturas seguidas enfileirem a
        # mesma Note duas vezes.
        app.state.em_revisao = set()
        app.state.revisao_sem = asyncio.Semaphore(REVISOES_SIMULTANEAS)
        app.state.conn = connect(db_path)
        app.state.home = Home(cfg.ha_url, cfg.ha_token)
        # `lighter` é injetável pelo mesmo motivo que `calendar`: sem isso, cada
        # teste que sobe o app roda `gsettings` de verdade e mexe nas
        # configurações da extensão do usuário — inclusive deixando
        # `auto-switch` ligado no shutdown. Teste não toca o desktop.
        app.state.lighter = lighter if lighter is not None else Lighter()
        app.state.notify = Notifier()
        app.state.calendar = calendar if calendar is not None else Calendar()
        app.state.llm = LLM(cfg.gemini_api_key)
        app.state.cal_adapter = CalendarAdapter(app.state.calendar)

        report = engine.load_rules(rules_path)
        app.state.rules, app.state.rule_errors = report.rules, report.errors
        for arquivo, _ in report.errors:
            log.error("regra %s não carregou; as outras seguem", arquivo)

        _wire_engine(app)
        # Tira a autonomia da extensão enquanto o daemon comanda, para o
        # WindowWatcher dela não sobrescrever o que uma Rule acabou de fazer.
        await app.state.lighter.take_over()

        tarefas = []
        if background:
            # Aquece a agenda em background: uma fonte recém-criada paga até 10s
            # para conectar, e o cache de clientes torna isso um custo único —
            # mas só se alguém pagar antes do usuário digitar `ta today`.
            app.state.warm_task = asyncio.create_task(
                asyncio.to_thread(app.state.calendar.warm), name="warm-calendar"
            )
            tarefas.append(app.state.warm_task)
            # Os watchers ficam sob a mesma chave porque o de microfone roda
            # `pw-dump` em subprocesso a cada segundo. Num teste isso deixava um
            # `BaseSubprocessTransport` semi-destruído quando o loop fechava, e a
            # suíte travava na saída em ~1 de cada 3 execuções.
            tarefas += [
                asyncio.create_task(app.state.scheduler.run(), name="scheduler"),
                asyncio.create_task(app.state.mic.run(), name="mic"),
                asyncio.create_task(app.state.state_watcher.run(), name="state"),
            ]
        log.info(
            "daemon de pé em %s | %d regra(s), %d erro(s)",
            cfg.base_url, len(app.state.rules), len(app.state.rule_errors),
        )
        try:
            yield
        finally:
            for t in tarefas:
                t.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            await app.state.lighter.hand_back()
            await app.state.home.close()
            app.state.conn.close()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", board),
            Route("/board", board),
            Route("/health", health),
            Route("/notes", notes_create, methods=["POST"]),
            Route("/notes", notes_list, methods=["GET"]),
            Route("/notes/{note_id:int}/move", notes_move, methods=["POST"]),
            Route("/notes/{note_id:int}/done", notes_done, methods=["POST"]),
            Route("/notes/{note_id:int}/status", notes_status, methods=["POST"]),
            Route("/notes/{note_id:int}", notes_delete, methods=["DELETE"]),
            Route("/notes/{note_id:int}/restore", notes_restore, methods=["POST"]),
            Route("/notes/{note_id:int}/purge", notes_purge, methods=["DELETE"]),
            Route("/trash", trash_purge, methods=["DELETE"]),
            Route("/review-all", review_all, methods=["POST"]),
            Route("/review-status", review_status),
            Route("/export", export),
            Route("/today", today),
            Route("/home/light", home_light, methods=["POST"]),
            Route("/home/off", home_off, methods=["POST"]),
            Route("/home/entities", home_entities),
            Route("/sensors", sensors_route),
            Route("/media", media, methods=["POST"]),
            Route("/lighter", lighter_route, methods=["POST"]),
            Route("/rules", rules_route),
            Route("/organize", organize, methods=["POST"]),
            Route("/detect-event", detect_event, methods=["POST"]),
            Route("/calendar/event", create_event, methods=["POST"]),
            Route("/priorities", priorities_route, methods=["GET", "POST"]),
            Route("/digest-prose", digest_prose, methods=["POST"]),
            Route("/models", models_route),
        ],
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

