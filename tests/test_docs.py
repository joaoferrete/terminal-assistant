"""A documentação de automações é código, e código na doc apodrece calado.

Este teste existe porque uma receita que não carrega é pior que receita nenhuma:
quem copiar leva o erro para casa achando que o problema é dele. Ele já pegou um
bloco do `when=` que estava sem os imports.
"""
import re
from pathlib import Path

import pytest

from ta.actuators.home import Home
from ta.actuators.lighter import Lighter
from ta.actuators.notify import Notifier
from ta.daemon import CalendarAdapter
from ta.engine import Context, load_rules

GUIA = Path(__file__).resolve().parents[1] / "docs" / "automacoes.md"


def blocos_python() -> list[str]:
    return re.findall(r"```python\n(.*?)```", GUIA.read_text(), re.S)


def test_o_guia_tem_receitas():
    """Guarda contra o teste passar porque não achou nada para testar."""
    assert len([b for b in blocos_python() if "@rule" in b]) >= 6


def test_toda_receita_do_guia_carrega(tmp_path):
    receitas = [b for b in blocos_python() if "@rule" in b]
    for i, bloco in enumerate(receitas):
        (tmp_path / f"receita_{i:02d}.py").write_text(bloco)

    rep = load_rules(tmp_path)
    assert rep.errors == [], f"receita do guia não carrega:\n{rep.errors}"
    assert len(rep.rules) >= len(receitas)


# Os métodos que o guia promete em `ctx.*`. Se um for renomeado, o guia mente.
@pytest.mark.parametrize(
    ("classe", "metodos"),
    [
        (Home, ("switch_on", "turn_off", "state", "entities", "sensors", "light")),
        (Lighter, ("apply_profile", "enable", "toggle", "profiles")),
        (Notifier, ("send",)),
        (CalendarAdapter, ("agora", "hoje")),
    ],
)
def test_a_api_documentada_existe(classe, metodos):
    for m in metodos:
        assert callable(getattr(classe, m, None)), f"{classe.__name__}.{m} não existe"


def test_os_campos_de_ctx_documentados_existem():
    campos = set(Context.__dataclass_fields__)
    assert {"now", "trigger", "home", "lighter", "notify", "calendar", "note", "extra"} <= campos


# ── Identidade de conta da agenda ───────────────────────────────────────────
from ta.sensors.calendar import CalendarSource  # noqa: E402


def test_conta_pessoal_reconhecida_pelo_provedor():
    for email in ("eu@gmail.com", "x@outlook.com", "y@icloud.com"):
        s = CalendarSource(uid="u", name="Terminal Assistant", parent="p", local=False,
                           account=email)
        assert s.personal, email


def test_dominio_proprio_e_tratado_como_trabalho():
    s = CalendarSource(uid="u", name="Terminal Assistant", parent="p", local=False,
                       account="eu@empresa-com-dominio.com")
    assert not s.personal


def test_conta_desconhecida_nao_e_pessoal():
    """Sem e-mail, não afirma que é pessoal — o padrão errado seria pior aqui."""
    s = CalendarSource(uid="u", name="x", parent="p", local=False, account="")
    assert not s.personal


# ── O perfil do `ta init` chega ao prompt da revisão ────────────────────────
# Este teste existe porque "estamos passando o contexto?" só se responde olhando
# o prompt de verdade: o dado existir no banco não prova que ele chegou lá.
import asyncio  # noqa: E402

from ta.llm import LLM  # noqa: E402


class LLMEspiao(LLM):
    """Intercepta `_structured` para inspecionar o prompt montado."""

    def __init__(self):
        super().__init__("chave-falsa")
        self.prompt = ""

    async def _structured(self, prompt, schema, system=""):
        self.prompt = prompt
        from types import SimpleNamespace

        return SimpleNamespace(
            intent="anotacao", due="", remind_at="", priority="", tags=[],
            is_event=False, title="", start="", end="", account="pessoal",
            confidence=0.0, reason="",
        )


PERFIL = "## Trabalho\nbackend de telemetria de frotas (Kafka, Cassandra, Go)"


def test_o_perfil_de_priorities_entra_no_prompt_da_revisao():
    espiao = LLMEspiao()
    asyncio.run(
        espiao.review_capture(
            "revisar o consumer do Kafka",
            due=None,
            remind_at=None,
            priorities=PERFIL,
            contas="pessoal: gmail.com, trabalho: cobli.co",
        )
    )
    assert "telemetria de frotas" in espiao.prompt
    assert "Kafka, Cassandra, Go" in espiao.prompt


def test_apenas_o_dominio_da_conta_vai_para_o_modelo():
    """Domínio decide o roteamento; e-mail inteiro seria dado a mais."""
    espiao = LLMEspiao()
    asyncio.run(
        espiao.review_capture(
            "x", due=None, remind_at=None,
            contas="pessoal: gmail.com, trabalho: cobli.co",
        )
    )
    assert "cobli.co" in espiao.prompt
    assert "eu@cobli.co" not in espiao.prompt


def test_sem_perfil_o_prompt_nao_ganha_secao_vazia():
    espiao = LLMEspiao()
    asyncio.run(espiao.review_capture("x", due=None, remind_at=None))
    assert "Contexto de quem escreveu" not in espiao.prompt


def test_marcadores_do_cli_e_do_export_sao_os_mesmos():
    """Divergir faria a mesma nota parecer diferente em `ta list` e `ta export`."""
    from ta.cli import STATUS_MARK as cli_marks
    from ta.store import STATUS_MARK as export_marks

    assert cli_marks == export_marks
