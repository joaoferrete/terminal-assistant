import pytest
from starlette.testclient import TestClient

from ta.config import Config
from ta.daemon import create_app


class FakeCalendar:
    """Agenda de mentira. Teste não fala com o Evolution Data Server do usuário."""

    available = True
    error = None

    def today(self, dia=None):
        return []

    def now(self, momento=None):
        return None

    def write_targets(self):
        return []

    def warm(self):
        return 0


class FakeLighter:
    """Sem isto, o teste escrevia em `org.gnome.shell.extensions.lighter` de
    verdade: `take_over` no boot e `hand_back` no shutdown, uma vez por teste."""

    available = True

    async def get(self, key):
        return "false"

    async def set(self, key, value):
        return True

    async def enable(self, on=True):
        return True

    async def toggle(self):
        return True

    async def apply_profile(self, nome, *, enable=True):
        return True

    async def take_over(self):
        pass

    async def hand_back(self):
        pass


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Config(ha_token="fake", gemini_api_key="fake", auto_review=False),
        db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "sem-regras",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        yield c


def test_health_reporta_presenca_nao_valor(client):
    """O /health existe para responder 'o serviço leu o .env?' sem vazar segredo."""
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["ha"]["token_configured"] is True
    assert body["gemini"]["key_configured"] is True
    # O valor nunca aparece.
    assert "fake" not in client.get("/health").text


def test_health_sem_segredos(tmp_path):
    app = create_app(
        Config(auto_review=False),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["ha"]["token_configured"] is False
    assert body["gemini"]["key_configured"] is False


def test_captura_via_http_extrai_papeis(client):
    r = client.post("/notes", json={"text": "ligar dentista @2026-12-25 #saude !alta"})
    assert r.status_code == 201
    n = r.json()
    assert n["text"] == "ligar dentista"
    assert n["roles"] == {"task": True, "reminder": False}
    assert n["due"] == "2026-12-25"
    assert n["tags"] == ["saude"]


def test_texto_vazio_e_400(client):
    assert client.post("/notes", json={"text": "   "}).status_code == 400


def test_mover_e_colorir_persiste_e_marca_pinned(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    r = client.post(f"/notes/{note_id}/move", json={"pos_x": 300, "pos_y": 150, "color": "#bfdcf5"})
    n = r.json()
    assert n["pos"] == [300, 150]
    assert n["color"] == "#bfdcf5"
    assert n["pinned_by_user"] is True


def test_done_alterna_nos_dois_sentidos(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    assert client.post(f"/notes/{note_id}/done").json()["done"] is True
    assert client.post(f"/notes/{note_id}/done", json={"done": False}).json()["done"] is False


def test_list_esconde_concluidas_por_padrao(client):
    a = client.post("/notes", json={"text": "aberta"}).json()["id"]
    b = client.post("/notes", json={"text": "fechada"}).json()["id"]
    client.post(f"/notes/{b}/done")
    assert [n["id"] for n in client.get("/notes").json()["notes"]] == [a]
    assert len(client.get("/notes?done=1").json()["notes"]) == 2


def test_board_e_servido_na_raiz_e_em_board(client):
    for path in ("/", "/board"):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Mural" in r.text


def test_export_markdown_via_http(client):
    client.post("/notes", json={"text": "ligar dentista @2026-12-25"})
    body = client.get("/export").text
    assert "- [ ] ligar dentista" in body
    assert "prazo 2026-12-25" in body


def test_status_via_http_e_done_at_exposto(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    n = client.post(f"/notes/{note_id}/status", json={"status": "done"}).json()
    assert n["status"] == "done"
    assert n["terminal"] is True
    assert n["done_at"] is not None      # o carimbo tem que chegar ao cliente


def test_cancelar_e_terminal_mas_nao_done(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    n = client.post(f"/notes/{note_id}/status", json={"status": "cancelled"}).json()
    assert (n["status"], n["terminal"], n["done"]) == ("cancelled", True, False)
    assert n["done_at"] is None          # cancelada não foi feita


def test_status_invalido_e_400(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    r = client.post(f"/notes/{note_id}/status", json={"status": "quase_feito"})
    assert r.status_code == 400
    assert "quase_feito" in r.json()["error"]


def test_doing_e_hold_continuam_na_fila(client):
    a = client.post("/notes", json={"text": "a"}).json()["id"]
    b = client.post("/notes", json={"text": "b"}).json()["id"]
    client.post(f"/notes/{a}/status", json={"status": "doing"})
    client.post(f"/notes/{b}/status", json={"status": "hold"})
    ids = [n["id"] for n in client.get("/notes").json()["notes"]]
    assert sorted(ids) == sorted([a, b])


class HomeDeMentira:
    """Registra as chamadas em vez de falar com o HA."""

    def __init__(self, inventario=None):
        self.chamadas = []
        self.apagados = []
        self.midia = []
        self.inventario = inventario or [
            {
                "entity_id": "light.lampada_do_quarto",
                "state": "on",
                "attributes": {"friendly_name": "Quarto"},
            }
        ]

    async def entities(self, *prefixes):
        return self.inventario

    async def turn_off(self, entity_id):
        self.apagados.append(entity_id)

    async def media(self, entity_id, action):
        self.midia.append((entity_id, action))

    async def switch_on(self, entity_id, brightness_pct=None):
        self.chamadas.append((entity_id, brightness_pct))

    async def confirm(self, entity_id, esperado, tries=12):
        return esperado, True

    async def close(self):
        pass


def test_on_sem_brilho_acende_no_maximo(client):
    """`ta on quarto` sem número é 'liga a luz', não 'liga fraco'."""
    fake = HomeDeMentira()
    client.app.state.home = fake
    r = client.post("/home/light", json={"entity": "quarto"})
    assert r.status_code == 200
    assert fake.chamadas == [("light.lampada_do_quarto", 100)]


def test_on_com_brilho_explicito_respeita_o_valor(client):
    fake = HomeDeMentira()
    client.app.state.home = fake
    client.post("/home/light", json={"entity": "quarto", "brightness": 30})
    assert fake.chamadas == [("light.lampada_do_quarto", 30)]


def test_off_sem_alvo_nao_mexe_no_travamento_infantil(client):
    """A varredura do `ta off` também é um grupo, e vale a mesma regra."""
    fake = HomeDeMentira([
        {"entity_id": "light.lampada_do_quarto", "state": "on", "attributes": {}},
        {
            "entity_id": "switch.ventilador_socket_1",
            "state": "on",
            "attributes": {"device_class": "outlet"},
        },
        {"entity_id": "switch.ventilador_bloqueio_para_criancas", "state": "on", "attributes": {}},
    ])
    client.app.state.home = fake
    r = client.post("/home/off", json={})
    assert r.status_code == 200
    assert fake.apagados == ["light.lampada_do_quarto", "switch.ventilador_socket_1"]


def test_media_sem_echo_configurado_explica_o_que_falta(client):
    """Sem TA_ECHOS o HA devolvia um 400 sem corpo útil, e o erro chegava ilegível."""
    r = client.post("/media", json={"action": "pause"})
    assert r.status_code == 400
    assert "TA_ECHOS" in r.json()["error"]


def test_media_recusa_alvo_que_nao_e_media_player(client):
    r = client.post("/media", json={"entity": "light.lampada_do_quarto", "action": "play"})
    assert r.status_code == 400
    assert "media_player" in r.json()["error"]


def test_media_usa_o_echo_do_config_quando_o_alvo_e_omitido(tmp_path):
    app = create_app(
        Config(ha_token="fake", echo_entities=("media_player.echo_quarto",)),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        fake = HomeDeMentira()
        c.app.state.home = fake
        r = c.post("/media", json={"action": "pause"})
        assert r.status_code == 200
        assert r.json()["entity_id"] == "media_player.echo_quarto"
        assert fake.midia == [("media_player.echo_quarto", "pause")]


# ── Segunda passada do LLM sobre a captura ──────────────────────────────────
# Testada como unidade, não pela rota: a revisão roda solta em background, e
# esperar por task de fundo dentro do TestClient é receita de teste instável.
from types import SimpleNamespace  # noqa: E402

from ta import db, store  # noqa: E402
from ta.daemon import _revisar_captura  # noqa: E402


class LLMDeMentira:
    """Devolve uma revisão fixa. Teste não fala com o Gemini."""

    configured = True

    def __init__(self, review):
        self.review = review
        self.chamadas = []

    async def review_capture(self, text, *, due, remind_at, priorities="", contas=""):
        self.chamadas.append((text, due, remind_at, priorities, contas))
        return self.review


class NotifyDeMentira:
    def __init__(self):
        self.avisos = []

    async def send(self, title, body="", *, urgency="normal", icon=None):
        self.avisos.append((title, body))
        return True


class Revisao(SimpleNamespace):
    """O mínimo do schema que `_revisar_captura` consome."""

    def __init__(self, **kw):
        super().__init__(
            intent=kw.get("intent", "anotacao"),
            due=kw.get("due", ""),
            remind_at=kw.get("remind_at", ""),
            priority=kw.get("priority", ""),
            tags=kw.get("tags", []),
            is_event=kw.get("is_event", False),
            title=kw.get("title", ""),
            start=kw.get("start", ""),
            end=kw.get("end", ""),
            account=kw.get("account", "pessoal"),
            confidence=kw.get("confidence", 0.9),
            reason=kw.get("reason", ""),
        )


PESSOAL = SimpleNamespace(uid="src-pessoal", name="Terminal Assistant", personal=True,
                            account="eu@gmail.com")
TRABALHO = SimpleNamespace(uid="src-trabalho", name="Terminal Assistant", personal=False,
                             account="eu@empresa.co")


def _app_falso(tmp_path, review, *, alvos=()):
    conn = db.connect(tmp_path / "t.db")
    cal = SimpleNamespace(write_targets=lambda: list(alvos), create_event=lambda *a: "uid-1")
    import asyncio as _asyncio
    return SimpleNamespace(
        state=SimpleNamespace(
            conn=conn,
            llm=LLMDeMentira(review),
            notify=NotifyDeMentira(),
            calendar=cal,
            em_revisao=set(),
            revisao_sem=_asyncio.Semaphore(4),
        )
    )


async def test_revisao_remove_prazo_que_o_regex_inventou(tmp_path):
    """O regex marca prazo pela forma; a revisão desfaz quando a intenção não é essa."""
    app = _app_falso(tmp_path, Revisao(intent="anotacao", due="", confidence=0.95,
                                       reason="é um registro, não um prazo"))
    nota = store.add_note(app.state.conn, "hoje eu preciso disso")
    assert nota.due is not None                       # o regex marcou

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).due is None
    assert "prazo removido" in app.state.notify.avisos[0][1]


async def test_revisao_com_confianca_baixa_nao_mexe_em_nada(tmp_path):
    """Correção errada é pior que nenhuma: abaixo do piso, não age."""
    app = _app_falso(tmp_path, Revisao(due="", confidence=0.3))
    nota = store.add_note(app.state.conn, "revisar o PR hoje")
    antes = nota.due

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).due == antes
    assert app.state.notify.avisos == []


async def test_revisao_cria_evento_na_agenda_dedicada(tmp_path):
    app = _app_falso(
        tmp_path,
        Revisao(intent="compromisso", is_event=True, title="nutricionista",
                start="2026-09-17T08:30", end="2026-09-17T09:30", confidence=0.95),
        alvos=(PESSOAL,),
    )
    nota = store.add_note(app.state.conn, "ir na nutricionista 17 de setembro as 8:30")

    await _revisar_captura(app, nota.id)

    assert "evento criado: nutricionista" in app.state.notify.avisos[0][1]
    link = app.state.conn.execute(
        "SELECT uid, source_uid FROM calendar_links WHERE note_id = ?", (nota.id,)
    ).fetchone()
    assert (link["uid"], link["source_uid"]) == ("uid-1", "src-pessoal")


async def test_sem_agenda_dedicada_nao_cria_evento_em_outro_lugar(tmp_path):
    """Sem a agenda 'Terminal Assistant', não escreve na principal. Guarda do ADR 0007."""
    app = _app_falso(
        tmp_path,
        Revisao(is_event=True, title="x", start="2026-09-17T08:30", confidence=0.99),
        alvos=(),
    )
    nota = store.add_note(app.state.conn, "compromisso qualquer")

    await _revisar_captura(app, nota.id)

    assert app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"] == 0


async def test_revisao_com_data_invalida_do_modelo_nao_estoura(tmp_path):
    app = _app_falso(
        tmp_path,
        Revisao(is_event=True, title="x", start="semana que vem", confidence=0.99),
        alvos=(PESSOAL,),
    )
    nota = store.add_note(app.state.conn, "algo")

    await _revisar_captura(app, nota.id)   # não deve levantar

    assert app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"] == 0


def test_revisao_desligada_nao_agenda_nada(tmp_path):
    """`TA_AUTO_REVIEW=0` tem que deixar o caminho do regex intocado."""
    app = create_app(
        Config(gemini_api_key="fake", auto_review=False),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        c.post("/notes", json={"text": "x"})
        assert c.app.state.revisoes == set()



async def test_evento_de_trabalho_vai_para_a_conta_de_trabalho(tmp_path):
    """Roteava por `parent`, um hash opaco: tudo caía na mesma agenda, calado."""
    app = _app_falso(
        tmp_path,
        Revisao(is_event=True, title="1:1 com a lead", start="2026-08-11T14:00",
                account="trabalho", confidence=0.95),
        alvos=(PESSOAL, TRABALHO),
    )
    nota = store.add_note(app.state.conn, "1:1 com a lead na terça às 14h")

    await _revisar_captura(app, nota.id)

    link = app.state.conn.execute("SELECT source_uid FROM calendar_links").fetchone()
    assert link["source_uid"] == "src-trabalho"


async def test_evento_pessoal_vai_para_a_conta_pessoal(tmp_path):
    app = _app_falso(
        tmp_path,
        Revisao(is_event=True, title="nutricionista", start="2026-09-17T08:30",
                account="pessoal", confidence=0.95),
        alvos=(TRABALHO, PESSOAL),   # ordem invertida: não pode ser "o primeiro"
        )
    nota = store.add_note(app.state.conn, "nutricionista 17 de setembro às 8:30")

    await _revisar_captura(app, nota.id)

    link = app.state.conn.execute("SELECT source_uid FROM calendar_links").fetchone()
    assert link["source_uid"] == "src-pessoal"


def test_review_all_enfileira_e_responde_quantas(client):
    """O botão do mural bate aqui. Sem LLM configurada devolve 400 legível."""
    client.post("/notes", json={"text": "a"})
    client.post("/notes", json={"text": "b"})
    r = client.post("/review-all")
    assert r.status_code == 400            # auto_review=False na fixture
    assert "TA_AUTO_REVIEW" in r.json()["error"]


def test_review_status_conta_a_fila(client):
    client.post("/notes", json={"text": "a"})
    body = client.get("/review-status").json()
    assert body == {"pending": 1, "running": 0}


def test_review_all_sem_chave_do_gemini(tmp_path):
    app = create_app(
        Config(),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        r = c.post("/review-all")
        assert r.status_code == 400
        assert "GEMINI_API_KEY" in r.json()["error"]


async def test_revisao_em_voo_nao_e_enfileirada_duas_vezes(tmp_path):
    """Quatro capturas seguidas revisavam a MESMA nota 4 vezes, com respostas
    diferentes. `em_revisao` é a guarda."""
    from ta.daemon import _agendar_revisao

    conn = db.connect(tmp_path / "t.db")
    nota = store.add_note(conn, "x")
    app = SimpleNamespace(
        state=SimpleNamespace(
            conn=conn,
            llm=SimpleNamespace(configured=True),
            auto_review=True,
            revisoes=set(),
            em_revisao={nota.id},        # já em voo
        )
    )
    _agendar_revisao(app, nota)
    assert app.state.revisoes == set()   # nada de novo foi criado


async def test_anotacao_nao_recebe_prioridade(tmp_path):
    """Registro e ideia solta não são cobráveis: prioridade neles só suja o mural."""
    app = _app_falso(tmp_path, Revisao(intent="anotacao", priority="alta", confidence=0.9))
    nota = store.add_note(app.state.conn, "hoje descobri o bug do domínio")

    await _revisar_captura(app, nota.id)

    n = store.get_note(app.state.conn, nota.id)
    assert n.priority is None
    assert "anotacao" in n.tags


async def test_anotacao_remove_prioridade_posta_por_maquina(tmp_path):
    """Reclassificar para anotação tem que desfazer a prioridade da passada anterior."""
    app = _app_falso(tmp_path, Revisao(intent="anotacao", confidence=0.9))
    nota = store.add_note(app.state.conn, "x")
    store.set_priority(app.state.conn, nota.id, "media")

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).priority is None


async def test_prioridade_digitada_pelo_usuario_e_intocavel(tmp_path):
    """`!alta` é seu. Nem uma reclassificação para anotação o apaga."""
    app = _app_falso(tmp_path, Revisao(intent="anotacao", priority="baixa", confidence=0.99))
    nota = store.add_note(app.state.conn, "revisar isso !alta")
    assert nota.priority_by_user

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).priority == "alta"


async def test_tema_digitado_pelo_usuario_sobrevive_e_ganha_os_eixos(tmp_path):
    """Tema é seu, eixo é estrutura.

    A primeira versão tratava suas tags como tudo-ou-nada: escrever `#app` fazia
    a nota perder área e tipo, e ela desaparecia dos dois filtros do mural.
    Aconteceu numa nota real.
    """
    app = _app_falso(
        tmp_path,
        Revisao(intent="anotacao", account="pessoal", tags=["estudo"], confidence=0.99),
    )
    nota = store.add_note(app.state.conn, "fiz um novo app hoje #app")
    assert nota.tags_by_user and nota.tags == ["app"]

    await _revisar_captura(app, nota.id)

    tags = store.get_note(app.state.conn, nota.id).tags
    assert "app" in tags                       # o seu tema fica
    assert {"pessoal", "anotacao"} <= set(tags)  # e os eixos entram
    assert "estudo" not in tags                # o tema do modelo NÃO substitui o seu


async def test_area_e_tipo_entram_sempre_como_tag(tmp_path):
    """Nota sem área nem tipo fica invisível nos filtros do mural."""
    app = _app_falso(
        tmp_path,
        Revisao(intent="tarefa", account="trabalho", tags=[], confidence=0.9),
    )
    nota = store.add_note(app.state.conn, "revisar o PR")

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).tags == ["tarefa", "trabalho"]


async def test_tema_inventado_pelo_modelo_e_descartado(tmp_path):
    """Vocabulário fechado: 'profissional' e 'job' virariam sinônimos de trabalho."""
    app = _app_falso(
        tmp_path,
        Revisao(intent="tarefa", tags=["profissional", "job", "estudo"], confidence=0.9),
    )
    nota = store.add_note(app.state.conn, "x")

    await _revisar_captura(app, nota.id)

    assert store.get_note(app.state.conn, nota.id).tags == ["estudo", "pessoal", "tarefa"]


async def test_no_maximo_dois_temas_alem_dos_eixos(tmp_path):
    app = _app_falso(
        tmp_path,
        Revisao(intent="tarefa", tags=["saude", "casa", "compras", "estudo"], confidence=0.9),
    )
    nota = store.add_note(app.state.conn, "x")

    await _revisar_captura(app, nota.id)

    tags = store.get_note(app.state.conn, nota.id).tags
    assert len(tags) == 4          # área + tipo + 2 temas
    assert {"pessoal", "tarefa"} <= set(tags)


async def test_revisao_nao_cria_evento_duplicado(tmp_path):
    """Reetiquetar três vezes criou TRÊS compromissos idênticos na agenda: o
    INSERT OR REPLACE trocava o vínculo e órfanava o evento anterior."""
    app = _app_falso(
        tmp_path,
        Revisao(intent="compromisso", is_event=True, title="nutricionista",
                start="2026-09-17T08:30", confidence=0.95),
        alvos=(PESSOAL,),
    )
    nota = store.add_note(app.state.conn, "nutricionista 17 de setembro às 8:30")

    await _revisar_captura(app, nota.id)
    await _revisar_captura(app, nota.id)      # segunda passada, como no `revisar tudo`
    await _revisar_captura(app, nota.id)

    n = app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"]
    assert n == 1
    # E o aviso de "evento criado" sai uma vez só.
    criados = [a for a in app.state.notify.avisos if "evento criado" in a[1]]
    assert len(criados) == 1


def test_purge_recusa_nota_que_nao_esta_na_lixeira(client):
    """409 em vez de apagar de surpresa algo que o usuário achava seguro."""
    nid = client.post("/notes", json={"text": "viva"}).json()["id"]
    r = client.request("DELETE", f"/notes/{nid}/purge")
    assert r.status_code == 409
    assert "não está na lixeira" in r.json()["error"]
    assert client.get("/notes").json()["notes"][0]["id"] == nid


def test_purge_de_nota_na_lixeira_funciona(client):
    nid = client.post("/notes", json={"text": "x"}).json()["id"]
    client.request("DELETE", f"/notes/{nid}")
    r = client.request("DELETE", f"/notes/{nid}/purge")
    assert r.status_code == 200 and r.json() == {"purged": 1, "id": nid}
    assert client.get("/notes?deleted=1").json()["notes"] == []


def test_esvaziar_lixeira_exige_confirmacao(client):
    nid = client.post("/notes", json={"text": "x"}).json()["id"]
    client.request("DELETE", f"/notes/{nid}")

    r = client.request("DELETE", "/trash", json={})
    assert r.status_code == 400
    assert "não tem volta" in r.json()["error"]
    assert len(client.get("/notes?deleted=1").json()["notes"]) == 1   # nada foi apagado

    r = client.request("DELETE", "/trash", json={"confirmed": True})
    assert r.json() == {"purged": 1}
    assert client.get("/notes?deleted=1").json()["notes"] == []
