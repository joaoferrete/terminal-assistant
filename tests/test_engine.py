from datetime import datetime

import pytest

from ta import engine
from ta.engine import Context, Trigger


@pytest.fixture(autouse=True)
def registro_limpo():
    engine._REGISTRY.clear()
    yield
    engine._REGISTRY.clear()


def ctx(kind, value=None, hora="10:00"):
    h, m = (int(x) for x in hora.split(":"))
    return Context(trigger=Trigger(kind, value), now=datetime(2026, 8, 10, h, m))


# ── Carga isolada ───────────────────────────────────────────────────────────
def escrever(dir, nome, conteudo):
    (dir / nome).write_text(conteudo)


def test_regra_com_erro_de_sintaxe_nao_derruba_as_outras(tmp_path):
    """A mitigação prometida no ADR 0002."""
    escrever(tmp_path, "boa.py", """
from ta.engine import rule, mic_active
@rule(on=mic_active())
def boa(ctx): pass
""")
    escrever(tmp_path, "quebrada.py", "isso nao e python valido (((")

    rep = engine.load_rules(tmp_path)
    assert [r.name for r in rep.rules] == ["boa"]
    assert len(rep.errors) == 1
    assert rep.errors[0][0] == "quebrada.py"
    assert not rep.ok


def test_regra_que_chama_exit_nao_mata_o_processo(tmp_path):
    """Por isso a carga captura BaseException, não Exception."""
    escrever(tmp_path, "suicida.py", "raise SystemExit(1)")
    escrever(tmp_path, "boa.py", """
from ta.engine import rule, reminder_due
@rule(on=reminder_due())
def boa(ctx): pass
""")
    rep = engine.load_rules(tmp_path)
    assert [r.name for r in rep.rules] == ["boa"]
    assert len(rep.errors) == 1


def test_arquivo_com_underscore_e_ignorado(tmp_path):
    escrever(tmp_path, "_util.py", "raise RuntimeError('nao deveria carregar')")
    rep = engine.load_rules(tmp_path)
    assert rep.ok and rep.rules == []


def test_diretorio_inexistente_nao_e_erro(tmp_path):
    rep = engine.load_rules(tmp_path / "nao-existe")
    assert rep.ok and rep.rules == []


# ── Casamento de gatilho ────────────────────────────────────────────────────
def test_casamento_por_kind_e_value():
    @engine.rule(on=engine.mic_active())
    def so_ativo(c): ...

    @engine.rule(on=engine.mic_inactive())
    def so_inativo(c): ...

    regras = list(engine._REGISTRY)
    assert [r.name for r in engine.matching(regras, Trigger("mic", True))] == ["so_ativo"]
    assert [r.name for r in engine.matching(regras, Trigger("mic", False))] == ["so_inativo"]


def test_value_none_casa_qualquer_valor():
    @engine.rule(on=engine.reminder_due())
    def qualquer(c): ...

    regras = list(engine._REGISTRY)
    assert len(engine.matching(regras, Trigger("reminder", 7))) == 1
    assert len(engine.matching(regras, Trigger("reminder"))) == 1


# ── Condições ───────────────────────────────────────────────────────────────
def test_after_e_before():
    assert engine.after("16:00")(ctx("mic", True, hora="16:30"))
    assert not engine.after("16:00")(ctx("mic", True, hora="15:59"))
    assert engine.before("16:00")(ctx("mic", True, hora="15:59"))


def test_all_of_e_any_of():
    depois16, antes18 = engine.after("16:00"), engine.before("18:00")
    c = ctx("mic", True, hora="17:00")
    assert engine.all_of(depois16, antes18)(c)
    assert not engine.all_of(depois16, engine.before("16:30"))(c)
    assert engine.any_of(engine.before("16:30"), depois16)(c)


# ── Despacho ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_when_falso_nao_executa():
    executou = []

    @engine.rule(on=engine.mic_active(), when=engine.after("16:00"))
    def tarde(c):
        executou.append(True)

    regras = list(engine._REGISTRY)
    assert await engine.dispatch(regras, ctx("mic", True, hora="15:00")) == []
    assert executou == []
    assert await engine.dispatch(regras, ctx("mic", True, hora="16:01")) == ["tarde"]


@pytest.mark.asyncio
async def test_regra_async_e_sync_funcionam():
    chamadas = []

    @engine.rule(on=engine.mic_active(), name="sincrona")
    def s(c):
        chamadas.append("s")

    @engine.rule(on=engine.mic_active(), name="assincrona")
    async def a(c):
        chamadas.append("a")

    await engine.dispatch(list(engine._REGISTRY), ctx("mic", True))
    assert sorted(chamadas) == ["a", "s"]


@pytest.mark.asyncio
async def test_excecao_numa_regra_nao_impede_a_seguinte():
    ok = []

    @engine.rule(on=engine.mic_active(), name="explode")
    def explode(c):
        raise RuntimeError("boom")

    @engine.rule(on=engine.mic_active(), name="segue")
    def segue(c):
        ok.append(True)

    executadas = await engine.dispatch(list(engine._REGISTRY), ctx("mic", True))
    assert executadas == ["segue"]   # a que explodiu não conta como executada
    assert ok == [True]              # mas a seguinte rodou
