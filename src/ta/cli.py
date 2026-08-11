"""CLI: cliente HTTP fino do daemon.

Deliberadamente burro. Toda a lógica vive no daemon, para que só exista um lugar
onde as coisas acontecem. O único trabalho real aqui é falhar de forma legível
quando o daemon não está de pé — nunca travar, nunca despejar traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser

import httpx

from .config import Config

# O daemon responde rápido em tudo que é determinístico. As rotas que chamam o
# LLM são a exceção, e por isso o timeout é por chamada, não global: 5s é o certo
# para dizer "o daemon caiu", e seria errado para uma chamada de modelo.
TIMEOUT = 5.0
TIMEOUT_LLM = 120.0
# O primeiro `ta today` depois de um restart espera o aquecimento da agenda: são
# 8 fontes do Evolution e uma recém-criada paga até 10s para conectar. 5s ali
# fazia o comando falhar sempre logo após `systemctl restart`.
TIMEOUT_CAL = 60.0


class Problem(Exception):
    """Erro para mostrar ao usuário, sem traceback."""


def _request(
    cfg: Config,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    timeout: float = TIMEOUT,
) -> httpx.Response:
    url = f"{cfg.base_url}{path}"
    try:
        r = httpx.request(method, url, json=payload, timeout=timeout)
    except httpx.ConnectError as e:
        raise Problem(
            f"daemon não está respondendo em {cfg.base_url}.\n"
            "  systemctl --user status ta\n"
            "  systemctl --user start ta"
        ) from e
    except httpx.TimeoutException as e:
        raise Problem(f"daemon não respondeu em {timeout:.0f}s ({cfg.base_url}).") from e

    if r.status_code >= 400:
        try:
            detail = r.json().get("error", r.text)
        except json.JSONDecodeError:
            detail = r.text
        raise Problem(f"daemon recusou ({r.status_code}): {detail}")
    return r


# Os mesmos símbolos do `ta export`, para os dois não divergirem.
STATUS_MARK = {"todo": "[ ]", "doing": "[~]", "hold": "[-]", "done": "[x]", "cancelled": "[/]"}


def _fmt_note(n: dict) -> str:
    """Uma linha por Note, nomeando os papéis com o vocabulário do glossário."""
    marks = []
    if n["roles"]["task"]:
        marks.append(f"tarefa, prazo {n['due']}")
    if n["roles"]["reminder"]:
        marks.append(f"lembrete {n['remind_at']}")
    if n["priority"]:
        marks.append(f"prio {n['priority']}")
    if n["tags"]:
        marks.append(" ".join(f"#{t}" for t in n["tags"]))
    # Marcador por estado, não binário: uma nota cancelada saía como `[ ]` e se
    # lia como aberta. Os símbolos são os mesmos do `ta export`.
    box = STATUS_MARK.get(n["status"], "[ ]")
    suffix = f"  ({'; '.join(marks)})" if marks else ""
    return f"{box} #{n['id']} {n['text']}{suffix}"


def cmd_note(cfg: Config, args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        raise Problem('nada para capturar. Exemplo: ta note "ligar dentista @sexta #saude !alta"')
    note = _request(cfg, "POST", "/notes", {"text": text}).json()
    print(_fmt_note(note))
    return 0


def cmd_list(cfg: Config, args) -> int:
    suffix = "?done=1" if args.all else ""
    notes = _request(cfg, "GET", f"/notes{suffix}").json()["notes"]
    if not notes:
        print("nenhuma nota.")
        return 0
    for n in notes:
        print(_fmt_note(n))
    return 0


def cmd_done(cfg: Config, args) -> int:
    note = _request(cfg, "POST", f"/notes/{args.note_id}/done").json()
    print(_fmt_note(note))
    return 0


def cmd_export(cfg: Config, args) -> int:
    print(_request(cfg, "GET", "/export").text, end="")
    return 0


def cmd_board(cfg: Config, args) -> int:
    # Confirma que o daemon está de pé antes de abrir o navegador: uma aba com
    # erro de conexão é pior que uma mensagem no terminal.
    _request(cfg, "GET", "/health")
    url = f"{cfg.base_url}/board"
    print(f"abrindo {url}")
    webbrowser.open(url)
    return 0


def cmd_luz(cfg: Config, args) -> int:
    payload = {"entity": args.entity}
    if args.brightness is not None:
        payload["brightness"] = args.brightness
    # Timeout maior: confirmar o estado espera o round-trip da nuvem Tuya, e um
    # grupo multiplica isso pelo número de alvos.
    r = _request(cfg, "POST", "/home/light", payload, timeout=60.0).json()
    for res in r["results"]:
        aviso = "" if res.get("confirmed", True) else "  (enviado, estado não confirmado)"
        print(f"{res['entity_id']}: {res['state']}{aviso}")
    return 0


def cmd_on(cfg: Config, args) -> int:
    """Liga qualquer Entity. `ta luz` é o mesmo comando, mantido pela familiaridade."""
    return cmd_luz(cfg, args)


def _fmt_num(v, casas=1):
    try:
        return f"{float(v):.{casas}f}"
    except (TypeError, ValueError):
        return "?"


def cmd_router(cfg: Config, args) -> int:
    r = _request(cfg, "GET", "/sensors").json()["router"]
    print(f"  IP externo:  {r['external_ip'] or '?'}")
    print(f"  download:    {_fmt_num(r['download_kib_s'])} KiB/s")
    print(f"  upload:      {_fmt_num(r['upload_kib_s'])} KiB/s")
    return 0


def cmd_temp(cfg: Config, args) -> int:
    s = _request(cfg, "GET", "/sensors").json()
    w, o = s["weather"], s["outlet"]
    print(f"  {_fmt_num(w['temperature'])}{w['unit'] or '°C'}  {w['condition'] or ''}")
    if w["humidity"] is not None:
        print(f"  umidade {w['humidity']}%   vento {_fmt_num(w['wind_speed'])} km/h")
    # A tomada mede corrente: dá para dizer se o ventilador puxa energia de fato,
    # que é diferente de o interruptor estar ligado.
    if o["state"] is not None:
        puxando = (float(o["watts"] or 0) > 0.5)
        print(
            f"  ventilador: {o['state']}"
            f"{' (puxando ' + _fmt_num(o['watts']) + ' W)' if puxando else ' (sem consumo)'}"
        )
    return 0


def cmd_off(cfg: Config, args) -> int:
    r = _request(
        cfg, "POST", "/home/off", {"entity": args.entity} if args.entity else {}, timeout=30.0
    ).json()
    if not r["turned_off"]:
        print("nada estava aceso.")
        return 0
    for res in r.get("results", []):
        aviso = "" if res.get("confirmed", True) else "  (não confirmado)"
        print(f"apagado: {res['entity_id']}{aviso}")
    return 0


def cmd_entities(cfg: Config, args) -> int:
    for e in _request(cfg, "GET", "/home/entities").json()["entities"]:
        print(f"{e['state']:>8}  {e['entity_id']}")
    return 0


def cmd_lighter(cfg: Config, args) -> int:
    payload: dict = {"toggle": True} if args.action == "toggle" else {"on": args.action == "on"}
    if args.profile:
        payload = {"profile": args.profile}
    r = _request(cfg, "POST", "/lighter", payload).json()
    print(f"lighter: enabled={r['enabled']}")
    return 0


ACOES_MIDIA = ("play", "pause", "stop", "next", "previous")


def cmd_media(cfg: Config, args) -> int:
    """`ta media pause` e `ta media echo pause` são ambos válidos.

    Os dois positionals eram ambíguos para o argparse — `ta media pause` caía em
    `entity="pause"` e o HA devolvia um 400 sem explicação. Aqui a ação é
    reconhecida pelo valor, e o que não é ação é o alvo.
    """
    alvo, acao = "", None
    for termo in args.alvos:
        if termo in ACOES_MIDIA:
            acao = termo
        else:
            alvo = termo

    payload: dict = {"entity": alvo}
    if args.volume is not None:
        payload["volume"] = args.volume
    elif args.play:
        payload["play"] = " ".join(args.play)
    elif args.announce:
        payload["announce"] = " ".join(args.announce)
    else:
        payload["action"] = acao or "play"
    _request(cfg, "POST", "/media", payload)
    print("ok")
    return 0


# Rótulo de largura fixa, para as linhas alinharem sem tabela. Só formatação: a
# ordem vem pronta do daemon, e o CLI não tem mais tabela de rank própria.
PRIO_LABEL = {"high": "!ALTA", "medium": "!med ", "low": "!bax "}


def cmd_revise(cfg: Config, args) -> int:
    """O mesmo que o botão "revisar tudo" do mural.

    Devolve todas as Notes abertas à fila e acompanha até drenar. Concluídas e
    canceladas ficam de fora: revisar prazo de coisa encerrada gasta chamada de
    modelo por nada.
    """
    r = _request(cfg, "POST", "/review-all", timeout=TIMEOUT_LLM).json()
    total = r["queued"]
    if not total:
        print("nada aberto para revisar.")
        return 0
    print(f"{total} nota(s) na fila. Revisando…")

    import time

    ultimo = total
    for _ in range(240):                       # teto de ~8 min
        time.sleep(2)
        s = _request(cfg, "GET", "/review-status").json()
        if s["pending"] != ultimo:
            print(f"  faltam {s['pending']}")
            ultimo = s["pending"]
        if not s["pending"] and not s["running"]:
            break
    print("pronto. `ta list` para ver o resultado.")
    return 0


def cmd_rm(cfg: Config, args) -> int:
    """Apaga de forma reversível. `ta rm --list` mostra a lixeira."""
    if args.purge:
        return _purge(cfg, args)
    if args.list:
        notes = _request(cfg, "GET", "/notes?deleted=1").json()["notes"]
        if not notes:
            print("lixeira vazia.")
            return 0
        for n in notes:
            print(f"[x] #{n['id']} {n['text']}  (apagada {n['deleted_at']})")
        print("\n`ta restore <id>` traz de volta.")
        return 0
    if args.note_id is None:
        raise Problem("diga qual nota apagar, ou use `ta rm --list`.")
    n = _request(cfg, "DELETE", f"/notes/{args.note_id}").json()
    print(f"apagada: #{n['id']} {n['text']}  (`ta restore {n['id']}` desfaz)")
    return 0


def _purge(cfg: Config, args) -> int:
    """Apaga em definitivo. Só alcança o que já está na lixeira.

    Pergunta antes, e por padrão. Esta é a única operação do app que não tem
    volta, e uma confirmação é barata comparada a perder nota que você achava
    guardada. `--yes` existe para script; `ta` interativo sempre pergunta.
    """
    if args.note_id is not None:
        r = _request(cfg, "DELETE", f"/notes/{args.note_id}/purge")
        print(f"apagada em definitivo: #{args.note_id}")
        return 0

    notes = _request(cfg, "GET", "/notes?deleted=1").json()["notes"]
    if not notes:
        print("lixeira vazia.")
        return 0

    print(f"{len(notes)} nota(s) na lixeira:")
    for n in notes:
        print(f"  #{n['id']} {n['text'][:60]}")

    if not args.yes:
        print("\nIsto NÃO tem volta.", end=" ")
        try:
            resposta = input("Digite 'apagar' para confirmar: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelado.")
            return 1
        if resposta != "apagar":
            print("cancelado.")
            return 1

    r = _request(cfg, "DELETE", "/trash", {"confirmed": True}).json()
    print(f"{r['purged']} nota(s) apagada(s) em definitivo.")
    return 0


def cmd_restore(cfg: Config, args) -> int:
    n = _request(cfg, "POST", f"/notes/{args.note_id}/restore").json()
    print(f"de volta: #{n['id']} {n['text']}")
    return 0


def cmd_today(cfg: Config, args) -> int:
    """O Digest. Determinístico e instantâneo — o LLM não entra aqui (ADR 0003)."""
    d = _request(
        cfg, "GET", f"/today{'?date=' + args.date if args.date else ''}", timeout=TIMEOUT_CAL
    ).json()
    print(f"— {d['date']} —")
    if d.get("calendar_warming"):
        print("  (agenda estava aquecendo; as próximas chamadas são instantâneas)")
    if d.get("weather") and d["weather"].get("temperature") is not None:
        w = d["weather"]
        print(f"  {_fmt_num(w['temperature'])}{w['unit'] or '°C'}, {w['condition'] or ''}"
              f"{', umidade ' + str(w['humidity']) + '%' if w.get('humidity') is not None else ''}")

    if not d["calendar_available"]:
        print(f"  agenda indisponível: {d['calendar_error']}")
    elif not d["events"]:
        print("  agenda: nada marcado.")
    else:
        print("  agenda:")
        for e in d["events"]:
            quando = "dia inteiro" if e["all_day"] else f"{e['start'][11:]}–{e['end'][11:]}"
            print(f"    {quando:14} {e['summary']}")

    if not d["tasks"]:
        print("  tarefas: nada cobrável hoje.")
    else:
        print("  tarefas:")
        # O `ta` roda do mesmo source tree, então o CLI é sempre o código novo
        # enquanto o daemon é o do último restart. Sem esta linha, um daemon velho
        # daria KeyError em `horizon` — alto, mas inútil. Dizer o que fazer é
        # melhor que um traceback, e melhor que degradar calado.
        if "horizon" not in d["tasks"][0]:
            print("    (daemon desatualizado — rode: systemctl --user restart ta)")
        # Sem `sorted`: o daemon já devolve na ordem de exibição — atrasadas
        # primeiro, prioridade dentro da faixa (ADR 0010). O rótulo continua na
        # frente porque no fim da linha era fácil não ver.
        for n in d["tasks"]:
            atraso = "  ATRASADA" if n.get("horizon") == "vencida" else ""
            prio = PRIO_LABEL.get(n["priority"], "     ")   # 5 chars, sempre
            print(f"    {prio} #{n['id']} {n['text']}{atraso}")
    return 0


def cmd_rules(cfg: Config, args) -> int:
    """`ta rules check` valida sem subir o daemon; sem `check`, lista o que está no ar."""
    if args.check:
        # Carrega localmente, sem falar com o daemon: é a checagem que serve para
        # rodar antes de reiniciar o serviço.
        from pathlib import Path

        from .engine import load_rules

        rules_dir = Path(args.dir) if args.dir else Path.cwd() / "rules"
        rep = load_rules(rules_dir)
        for r in rep.rules:
            print(f"  ok   {r.name}  ({', '.join(str(t) for t in r.on)})")
        for arquivo, tb in rep.errors:
            print(f"  ERRO {arquivo}", file=sys.stderr)
            print("       " + tb.strip().splitlines()[-1], file=sys.stderr)
        print(f"\n{len(rep.rules)} regra(s), {len(rep.errors)} com erro.")
        return 0 if rep.ok else 1

    d = _request(cfg, "GET", "/rules").json()
    for r in d["rules"]:
        print(f"  {r['name']}  ({', '.join(r['on'])})")
    for e in d["errors"]:
        print(f"  ERRO {e['file']}", file=sys.stderr)
    return 0


def cmd_organize(cfg: Config, args) -> int:
    r = _request(cfg, "POST", "/organize", timeout=TIMEOUT_LLM).json()
    print(f"{r['placed']} nota(s) reorganizada(s) por {r['model']}")
    if r.get("skipped_pinned"):
        print(f"{r['skipped_pinned']} respeitada(s): você as arrastou à mão.")
    if r.get("groups"):
        print("grupos: " + " → ".join(r["groups"]))
    return 0


def cmd_prose(cfg: Config, args) -> int:
    print(_request(cfg, "POST", "/digest-prose", timeout=TIMEOUT_LLM).json()["text"])
    return 0


def cmd_init(cfg: Config, args) -> int:
    """Entrevista da primeira execução. Sem isto, 'prioridade' é chute."""
    atual = _request(cfg, "GET", "/priorities").json()
    if atual.get("content") and not args.force:
        print(atual["content"])
        print("\njá existe. Use --force para responder de novo, ou "
              '`ta priorities "instrução"` para ajustar por prompt.')
        return 0
    respostas = {}
    print("Quatro perguntas. Responder vazio deixa em branco.\n")
    for chave, pergunta in atual["questions"]:
        try:
            respostas[chave] = input(f"{pergunta}\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelado.")
            return 1
        print()
    r = _request(cfg, "POST", "/priorities", {"answers": respostas}).json()
    print(r["content"])
    return 0


def cmd_priorities(cfg: Config, args) -> int:
    if not args.instruction:
        c = _request(cfg, "GET", "/priorities").json()["content"]
        print(c or "(ainda não definido — rode `ta init`)")
        return 0
    r = _request(
        cfg, "POST", "/priorities", {"instruction": " ".join(args.instruction)},
        timeout=TIMEOUT_LLM,
    ).json()
    print(r["content"])
    return 0


def cmd_event(cfg: Config, args) -> int:
    """Propõe um evento a partir de uma nota, e só grava com o seu sim (ADR 0007)."""
    payload = {"note_id": args.note_id} if args.note_id else {"text": " ".join(args.text or [])}
    r = _request(cfg, "POST", "/detect-event", payload, timeout=TIMEOUT_LLM).json()
    c, alvos = r["candidate"], r["targets"]

    if not c["is_event"]:
        print("não parece um compromisso com data e hora.")
        return 0

    print("▶ evento detectado:")
    print(f"    título: {c['title']}")
    print(f"    quando: {c['start'].replace('T', ' ')} – {c['end'][11:]}")
    print(f"    agenda: Terminal Assistant ({c['account']})")
    print(f"    confiança: {c['confidence']:.0%}")
    if c["account"] not in alvos:
        raise Problem(f"não achei a agenda dedicada da conta {c['account']}.")

    try:
        resp = input("\n[s] criar  [n] só nota  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\ncancelado.")
        return 1
    if resp != "s":
        print("nada foi criado.")
        return 0

    out = _request(cfg, "POST", "/calendar/event", {
        "confirmed": True,
        "source_uid": alvos[c["account"]],
        "title": c["title"], "start": c["start"], "end": c["end"],
        "note_id": args.note_id,
    }).json()
    print(f"criado na agenda dedicada (uid {out['uid'][:12]}…), sem convidados.")
    return 0


def cmd_capture_popup(cfg: Config, args) -> int:
    """Captura rápida para o atalho global. Sem terminal aberto.

    Usa zenity se existir; sem ele, cai para notificação explicando o conserto —
    nunca falha em silêncio num atalho de teclado.
    """
    import shutil
    import subprocess

    zenity = shutil.which("zenity")
    if zenity is None:
        subprocess.run(
            ["notify-send", "Terminal Assistant", "Instale o zenity para a captura rápida."],
            check=False,
        )
        raise Problem("zenity não encontrado. `sudo apt install zenity`.")
    proc = subprocess.run(
        [zenity, "--entry", "--title=Nota", "--text=Nova nota:", "--width=460"],
        capture_output=True, text=True, check=False,
    )
    texto = proc.stdout.strip()
    if not texto:
        return 0
    note = _request(cfg, "POST", "/notes", {"text": texto}).json()
    subprocess.run(["notify-send", "Nota salva", _fmt_note(note)], check=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ta", description="Terminal Assistant")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("note", help="captura uma nota")
    n.add_argument("text", nargs="+")
    n.set_defaults(func=cmd_note)

    ls = sub.add_parser("list", help="lista notas abertas")
    ls.add_argument("--all", action="store_true", help="inclui concluídas")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("done", help="conclui uma nota")
    d.add_argument("note_id", type=int)
    d.set_defaults(func=cmd_done)

    rv = sub.add_parser("rm", help="apaga uma nota (reversível)")
    rv.add_argument("note_id", nargs="?", type=int)
    rv.add_argument("--list", action="store_true", help="mostra a lixeira")
    rv.add_argument(
        "--purge",
        action="store_true",
        help="apaga em definitivo o que está na lixeira (sem volta)",
    )
    rv.add_argument("--yes", action="store_true", help="não pergunta (para script)")
    rv.set_defaults(func=cmd_rm)

    rs = sub.add_parser("restore", help="tira uma nota da lixeira")
    rs.add_argument("note_id", type=int)
    rs.set_defaults(func=cmd_restore)

    sub.add_parser("revise", help="LLM reetiqueta todas as notas abertas").set_defaults(
        func=cmd_revise
    )

    sub.add_parser("export", help="despeja as notas em markdown").set_defaults(func=cmd_export)
    sub.add_parser("board", help="abre o mural no navegador").set_defaults(func=cmd_board)

    t = sub.add_parser("today", help="compromissos e tarefas do dia")
    t.add_argument("--date", help="AAAA-MM-DD (padrão: hoje)")
    t.set_defaults(func=cmd_today)

    lz = sub.add_parser("luz", help="acende/ajusta uma luz")
    lz.add_argument("entity", help="apelido (quarto) ou entity_id")
    lz.add_argument("brightness", nargs="?", type=int, help="0-100")
    lz.set_defaults(func=cmd_luz)

    of = sub.add_parser("off", help="apaga tudo, ou uma entity")
    of.add_argument("entity", nargs="?")
    of.set_defaults(func=cmd_off)

    on = sub.add_parser("on", help="liga entity, grupo (luz, tudo) ou ambiente (quarto)")
    on.add_argument("entity", help="entity_id, apelido, grupo ou nome de ambiente")
    on.add_argument("brightness", nargs="?", type=int, help="0-100, só em light.")
    on.set_defaults(func=cmd_on)

    sub.add_parser("entities", help="lista o inventário vindo do HA").set_defaults(
        func=cmd_entities
    )
    sub.add_parser("router", help="IP externo e velocidade").set_defaults(func=cmd_router)
    sub.add_parser("temp", help="temperatura, clima e consumo da tomada").set_defaults(
        func=cmd_temp
    )

    lg = sub.add_parser("lighter", help="controla a borda luminosa")
    lg.add_argument("action", nargs="?", choices=["on", "off", "toggle"], default="toggle")
    lg.add_argument("--profile", help="aplica um profile por nome")
    lg.set_defaults(func=cmd_lighter)

    md = sub.add_parser("media", help="mídia num Echo (media_player do HA)")
    md.add_argument(
        "alvos",
        nargs="*",
        metavar="[entity] [ação]",
        help=f"ação: {', '.join(ACOES_MIDIA)}. Sem entity, usa o TA_ECHOS",
    )
    md.add_argument("--volume", type=int)
    md.add_argument("--play", nargs="+", help="o que tocar")
    md.add_argument("--announce", nargs="+", help="texto para falar")
    md.set_defaults(func=cmd_media)

    sub.add_parser("organize", help="LLM agrupa e ordena, e grava").set_defaults(
        func=cmd_organize
    )
    sub.add_parser("prose", help="prosa do dia (opcional)").set_defaults(func=cmd_prose)
    sub.add_parser("capture-popup", help="captura rápida (para o atalho global)").set_defaults(
        func=cmd_capture_popup
    )

    ini = sub.add_parser("init", help="entrevista de prioridades")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init)

    pr = sub.add_parser("priorities", help="mostra ou ajusta as prioridades por prompt")
    pr.add_argument("instruction", nargs="*")
    pr.set_defaults(func=cmd_priorities)

    ev = sub.add_parser("event", help="propõe um evento a partir de uma nota")
    ev.add_argument("--note-id", type=int)
    ev.add_argument("text", nargs="*")
    ev.set_defaults(func=cmd_event)

    rl = sub.add_parser("rules", help="lista ou valida as regras")
    rl.add_argument("check", nargs="?", const=True, default=False, help="valida sem o daemon")
    rl.add_argument("--dir", help="diretório de regras")
    rl.set_defaults(func=cmd_rules)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(Config.from_env(), args)
    except Problem as e:
        print(f"ta: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
