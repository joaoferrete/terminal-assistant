from datetime import date, datetime

import pytest

from ta import db, store

NOW = datetime(2026, 8, 10, 9, 0)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_captura_nota_solta(conn):
    n = store.add_note(conn, "ideia: usar cache", now=NOW)
    assert n.text == "ideia: usar cache"
    assert not n.is_task and not n.is_reminder and not n.is_done
    assert n.tags == []


def test_captura_com_atributos_e_tags(conn):
    n = store.add_note(conn, "ligar dentista @sexta #saude #urgente !alta", now=NOW)
    assert n.text == "ligar dentista"
    assert n.due == "2026-08-14"
    assert n.priority == "alta"
    assert n.tags == ["saude", "urgente"]
    assert n.is_task


def test_sort_key_cresce_na_ordem_de_captura(conn):
    a = store.add_note(conn, "primeira", now=NOW)
    b = store.add_note(conn, "segunda", now=NOW)
    assert b.sort_key > a.sort_key


def test_arrastar_marca_pinned_by_user(conn):
    """O que faz a mão do usuário vencer o LLM de forma permanente (ADR 0003)."""
    n = store.add_note(conn, "x", now=NOW)
    assert not n.pinned_by_user
    store.move_note(conn, n.id, sort_key=0.5, pos_x=120, pos_y=40)
    moved = store.get_note(conn, n.id)
    assert moved.pinned_by_user
    assert (moved.sort_key, moved.pos_x, moved.pos_y) == (0.5, 120, 40)


def test_list_esconde_concluidas_por_padrao(conn):
    a = store.add_note(conn, "aberta", now=NOW)
    b = store.add_note(conn, "fechada", now=NOW)
    store.mark_done(conn, b.id, now=NOW)
    assert [n.id for n in store.list_notes(conn)] == [a.id]
    assert len(store.list_notes(conn, include_done=True)) == 2


def test_due_today_inclui_vencidas(conn):
    store.add_note(conn, "atrasada @2026-08-01", now=NOW)
    store.add_note(conn, "hoje @hoje", now=NOW)
    store.add_note(conn, "futura @2026-12-25", now=NOW)
    store.add_note(conn, "sem prazo", now=NOW)
    cobrarveis = store.due_today(conn, today=date(2026, 8, 10))
    assert [n.text for n in cobrarveis] == ["atrasada", "hoje"]


def test_pending_reminders_respeita_fired_at(conn):
    n = store.add_note(conn, "tomar remedio !!08:00", now=NOW)  # 08:00 < 09:00 -> amanha
    assert n.remind_at == "2026-08-11T08:00:00"
    # Nada pendente agora.
    assert store.pending_reminders(conn, now=NOW) == []
    # Depois da hora, pendente.
    depois = datetime(2026, 8, 11, 8, 1)
    assert [x.id for x in store.pending_reminders(conn, now=depois)] == [n.id]
    # Disparado, sai da fila — o scheduler não repete.
    store.mark_fired(conn, n.id, now=depois)
    assert store.pending_reminders(conn, now=depois) == []


def test_export_markdown(conn):
    store.add_note(conn, "ligar dentista @sexta #saude", now=NOW)
    b = store.add_note(conn, "feita", now=NOW)
    store.mark_done(conn, b.id, now=NOW)
    out = store.export_markdown(conn)
    assert "- [ ] ligar dentista" in out
    assert "prazo 2026-08-14" in out
    assert "#saude" in out
    assert "- [x] feita" in out


def test_tags_sem_n_mais_1(conn):
    """list_notes precisa buscar tags numa query só."""
    for i in range(5):
        store.add_note(conn, f"n{i} #a #b", now=NOW)
    notes = store.list_notes(conn)
    assert all(n.tags == ["a", "b"] for n in notes)


def test_status_nasce_todo(conn):
    assert store.add_note(conn, "x", now=NOW).status == "todo"


def test_set_status_valida_enum(conn):
    n = store.add_note(conn, "x", now=NOW)
    with pytest.raises(ValueError, match="status inválido"):
        store.set_status(conn, n.id, "quase_feito")


def test_done_at_e_carimbo_de_tempo_nao_estado(conn):
    """Entrar em done grava o instante; sair limpa; cancelar nunca grava."""
    n = store.add_note(conn, "x", now=NOW)
    store.set_status(conn, n.id, "done", now=NOW)
    assert store.get_note(conn, n.id).done_at == "2026-08-10T09:00:00"

    store.set_status(conn, n.id, "doing")
    assert store.get_note(conn, n.id).done_at is None

    store.set_status(conn, n.id, "cancelled")
    got = store.get_note(conn, n.id)
    assert got.done_at is None          # cancelada não foi feita
    assert got.is_terminal              # mas saiu da fila
    assert not got.is_done


def test_terminais_saem_da_lista_por_padrao(conn):
    a = store.add_note(conn, "aberta", now=NOW)
    b = store.add_note(conn, "feita", now=NOW)
    c = store.add_note(conn, "cancelada", now=NOW)
    d = store.add_note(conn, "em andamento", now=NOW)
    store.set_status(conn, b.id, "done", now=NOW)
    store.set_status(conn, c.id, "cancelled")
    store.set_status(conn, d.id, "doing")
    # doing e hold continuam na fila; done e cancelled saem.
    assert sorted(n.id for n in store.list_notes(conn)) == sorted([a.id, d.id])
    assert len(store.list_notes(conn, include_done=True)) == 4


def test_due_today_ignora_canceladas(conn):
    n = store.add_note(conn, "cancelada @hoje", now=NOW)
    store.set_status(conn, n.id, "cancelled")
    assert store.due_today(conn, today=date(2026, 8, 10)) == []


# ── Ordem de exibição: horizonte, e prioridade dentro dele (ADR 0010) ───────
# Todo teste daqui para baixo passa `today=` explícito. Um que dependesse de
# `date.today()` passaria em agosto de 2026 e falharia em 2027.
HOJE = NOW.date()   # 2026-08-10


def _ordem(conn, *, include_done=False):
    return [
        n.text
        for n in store.by_urgency(
            store.list_notes(conn, include_done=include_done), today=HOJE
        )
    ]


@pytest.mark.parametrize(
    ("due", "faixa"),
    [
        ("2026-08-09", "vencida"),   # ontem
        ("2026-08-10", "hoje"),
        ("2026-08-13", "semana"),
        ("2026-08-17", "semana"),    # dia 7 exato: dentro, o limite é inclusivo
        ("2026-08-18", "depois"),    # dia 8: fora
        (None, "depois"),            # sem prazo mora com o futuro distante
    ],
)
def test_horizon_das_faixas(due, faixa):
    assert store.horizon(due, today=HOJE) == faixa


def test_prazo_de_hoje_vence_prioridade_alta_de_semana_que_vem(conn):
    """O pedido que originou o ADR 0010, como frase executável.

    Antes, prioridade era o primeiro critério e a `!alta` distante ficava em cima.
    """
    store.add_note(conn, "RFC semana que vem !alta @2026-08-14", now=NOW)
    store.add_note(conn, "condominio hoje !media @2026-08-10", now=NOW)
    assert _ordem(conn) == ["condominio hoje", "RFC semana que vem"]


def test_vencida_vem_antes_de_hoje(conn):
    store.add_note(conn, "hoje !alta @2026-08-10", now=NOW)
    store.add_note(conn, "atrasada !baixa @2026-08-01", now=NOW)
    assert _ordem(conn) == ["atrasada", "hoje"]


def test_alta_sem_prazo_nao_fica_atras_de_baixa_de_setembro(conn):
    """Por que `sem prazo` mora em `depois` e não numa faixa própria no fim."""
    store.add_note(conn, "setembro !baixa @2026-09-01", now=NOW)
    store.add_note(conn, "sem data !alta", now=NOW)
    assert _ordem(conn) == ["sem data", "setembro"]


def test_prioridade_ordena_dentro_da_faixa(conn):
    store.add_note(conn, "sem prio", now=NOW)
    store.add_note(conn, "baixa !baixa", now=NOW)
    store.add_note(conn, "alta !alta", now=NOW)
    store.add_note(conn, "media !media", now=NOW)
    feita = store.add_note(conn, "feita !alta", now=NOW)
    store.set_status(conn, feita.id, "done", now=NOW)

    # Nenhuma tem prazo, então todas caem em `depois` e a prioridade decide.
    # Terminal vai pro fim mesmo sendo prioridade alta.
    assert _ordem(conn, include_done=True) == ["alta", "media", "baixa", "sem prio", "feita"]


def test_prazo_antes_de_sem_prazo_dentro_da_mesma_faixa(conn):
    """Guarda o termo `n.due is None` da chave.

    Sem ele, `n.due or ""` mapeia a nota sem data para `""`, que ordena antes de
    qualquer data ISO, e dentro de `depois` a ideia solta passaria na frente da
    tarefa datada.
    """
    store.add_note(conn, "sem prazo !alta", now=NOW)
    store.add_note(conn, "setembro !alta @2026-09-01", now=NOW)
    store.add_note(conn, "outubro !alta @2026-10-01", now=NOW)
    assert _ordem(conn) == ["setembro", "outubro", "sem prazo"]


def test_terminal_fica_no_fim_mesmo_vencida(conn):
    """Guarda `is_terminal` como PRIMEIRO termo, à frente do horizonte.

    Sem isso, uma nota concluída na semana passada — prazo no passado, logo
    `vencida` — subiria para o topo do quadro.
    """
    store.add_note(conn, "aberta sem prazo", now=NOW)
    feita = store.add_note(conn, "feita e vencida !alta @2026-08-01", now=NOW)
    store.set_status(conn, feita.id, "done", now=NOW)
    assert _ordem(conn, include_done=True) == ["aberta sem prazo", "feita e vencida"]


def test_dentro_da_faixa_o_sort_key_ainda_decide(conn):
    """A promessa do ADR 0003 sob teste: o que foi gravado continua valendo.

    Duas notas idênticas em faixa e prioridade — só o `sort_key` as separa.
    """
    a = store.add_note(conn, "primeira", now=NOW)
    b = store.add_note(conn, "segunda", now=NOW)
    assert _ordem(conn) == ["primeira", "segunda"]
    store.move_note(conn, b.id, sort_key=a.sort_key - 1)
    assert _ordem(conn) == ["segunda", "primeira"]


def test_export_mostra_estado_nao_binario(conn):
    a = store.add_note(conn, "andando", now=NOW)
    b = store.add_note(conn, "cancelada", now=NOW)
    store.set_status(conn, a.id, "doing")
    store.set_status(conn, b.id, "cancelled")
    out = store.export_markdown(conn)
    assert "[~] andando" in out
    assert "[/] cancelada" in out


def test_cor_vazia_volta_ao_padrao_da_prioridade(conn):
    """`color=""` limpa; `color=None` não mexe. São pedidos diferentes."""
    nid = store.add_note(conn, "x !alta", now=NOW).id
    store.move_note(conn, nid, color="#bfdcf5")
    assert store.get_note(conn, nid).color == "#bfdcf5"

    store.move_note(conn, nid, pos_x=10)          # None em color não apaga a cor
    assert store.get_note(conn, nid).color == "#bfdcf5"

    store.move_note(conn, nid, color="")          # agora sim
    assert store.get_note(conn, nid).color is None


# ── Fila de revisão ─────────────────────────────────────────────────────────
def test_nota_nova_nasce_pendente_de_revisao(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    assert store.pending_review(conn) == [nid]


def test_marcar_revisada_tira_da_fila(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.mark_reviewed(conn, nid, now=NOW)
    assert store.pending_review(conn) == []


def test_fila_ignora_quem_estourou_o_teto(conn):
    """Sem teto, uma nota que falha sempre seria retentada para sempre."""
    nid = store.add_note(conn, "x", now=NOW).id
    for _ in range(5):
        store.count_review_attempt(conn, nid)
    assert store.pending_review(conn, max_attempts=5) == []
    assert store.pending_review(conn, max_attempts=6) == [nid]


def test_fila_tem_limite_e_ordem_estavel(conn):
    """Uma semana offline não vira rajada: drena aos poucos, mais antigas antes."""
    ids = [store.add_note(conn, f"n{i}", now=NOW).id for i in range(5)]
    assert store.pending_review(conn, limit=2) == ids[:2]


def test_revisar_tudo_pega_abertas_e_ignora_terminais(conn):
    aberta = store.add_note(conn, "aberta", now=NOW).id
    feita = store.add_note(conn, "feita", now=NOW).id
    cancelada = store.add_note(conn, "cancelada", now=NOW).id
    for i in (aberta, feita, cancelada):
        store.mark_reviewed(conn, i, now=NOW)
    store.set_status(conn, feita, "done", now=NOW)
    store.set_status(conn, cancelada, "cancelled", now=NOW)

    assert store.queue_all_for_review(conn) == 1
    assert store.pending_review(conn) == [aberta]


def test_revisar_tudo_zera_tentativas(conn):
    """Pedido explícito do usuário é ordem nova, não continuação de desistência."""
    nid = store.add_note(conn, "x", now=NOW).id
    for _ in range(5):
        store.count_review_attempt(conn, nid)
    store.mark_reviewed(conn, nid, now=NOW)

    store.queue_all_for_review(conn)
    assert store.pending_review(conn, max_attempts=5) == [nid]


def test_set_tags_substitui_e_deduplica(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.set_tags(conn, nid, ["trabalho", "estudo", "trabalho"])
    assert store.get_note(conn, nid).tags == ["estudo", "trabalho"]
    store.set_tags(conn, nid, ["pessoal"])
    assert store.get_note(conn, nid).tags == ["pessoal"]


# ── Soft delete ─────────────────────────────────────────────────────────────
def test_apagada_sai_de_todas_as_listagens(conn):
    """Apagar tem que sumir de TODO caminho de leitura, não só do mural."""
    # `!!10:00` com NOW às 09:00 vence hoje; `!!09:00` cairia para amanhã.
    viva = store.add_note(conn, "viva @2026-08-10 !!10:00", now=NOW).id
    morta = store.add_note(conn, "morta @2026-08-10 !!10:00", now=NOW).id
    store.soft_delete(conn, morta, now=NOW)

    depois = datetime(2026, 8, 10, 11, 0)
    assert [n.id for n in store.list_notes(conn)] == [viva]
    assert [n.id for n in store.due_today(conn, today=date(2026, 8, 10))] == [viva]
    assert [n.id for n in store.pending_reminders(conn, now=depois)] == [viva]
    assert store.pending_review(conn) == [viva]
    assert store.queue_all_for_review(conn) == 1


def test_lixeira_devolve_so_as_apagadas(conn):
    viva = store.add_note(conn, "viva", now=NOW).id
    morta = store.add_note(conn, "morta", now=NOW).id
    store.soft_delete(conn, morta, now=NOW)

    assert [n.id for n in store.list_notes(conn, deleted=True)] == [morta]
    assert [n.id for n in store.list_notes(conn)] == [viva]


def test_restaurar_traz_de_volta(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.soft_delete(conn, nid, now=NOW)
    assert store.get_note(conn, nid).is_deleted

    store.restore(conn, nid)
    assert not store.get_note(conn, nid).is_deleted
    assert [n.id for n in store.list_notes(conn)] == [nid]


def test_apagar_e_cancelar_sao_eixos_independentes(conn):
    """`cancelled` é 'decidi não fazer' e fica no kanban; apagada sai de tudo.

    Um sexto status perderia essa distinção — dá para apagar uma nota concluída.
    """
    nid = store.add_note(conn, "x", now=NOW).id
    store.set_status(conn, nid, "done", now=NOW)
    store.soft_delete(conn, nid, now=NOW)

    n = store.get_note(conn, nid)
    assert n.status == "done" and n.is_deleted
    assert store.list_notes(conn, include_done=True) == []
    assert [x.id for x in store.list_notes(conn, deleted=True)] == [nid]


# ── Purge ───────────────────────────────────────────────────────────────────
def test_purge_so_alcanca_o_que_esta_na_lixeira(conn):
    """A segurança inteira do purge: não existe caminho que pule o soft delete."""
    viva = store.add_note(conn, "viva", now=NOW).id

    assert store.purge(conn, viva) is False
    assert store.get_note(conn, viva).text == "viva"      # continua lá

    store.soft_delete(conn, viva, now=NOW)
    assert store.purge(conn, viva) is True
    with pytest.raises(KeyError):
        store.get_note(conn, viva)


def test_purge_leva_as_tags_junto(conn):
    """`ON DELETE CASCADE` só funciona com `foreign_keys = ON`, que `connect` liga."""
    nid = store.add_note(conn, "x #casa #saude", now=NOW).id

    def quantas_tags():
        return conn.execute(
            "SELECT COUNT(*) c FROM tags WHERE note_id = ?", (nid,)
        ).fetchone()["c"]

    assert quantas_tags() == 2

    store.soft_delete(conn, nid, now=NOW)
    store.purge(conn, nid)

    assert quantas_tags() == 0


def test_esvaziar_lixeira_nao_toca_nas_vivas(conn):
    vivas = [store.add_note(conn, f"viva{i}", now=NOW).id for i in range(3)]
    mortas = [store.add_note(conn, f"morta{i}", now=NOW).id for i in range(2)]
    for i in mortas:
        store.soft_delete(conn, i, now=NOW)

    assert store.purge_all(conn) == 2
    assert sorted(n.id for n in store.list_notes(conn)) == sorted(vivas)
    assert store.list_notes(conn, deleted=True) == []


def test_esvaziar_lixeira_vazia_e_zero(conn):
    store.add_note(conn, "viva", now=NOW)
    assert store.purge_all(conn) == 0
