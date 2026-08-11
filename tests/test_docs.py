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


# ── A doc cita as regras que existem de verdade (ADR 0011) ──────────────────
REPO = Path(__file__).resolve().parents[1]
DOCS_COM_REGRAS = (REPO / "README.md", GUIA)


def regras_reais() -> set[str]:
    return {r.name for r in load_rules(REPO / "examples" / "rules").rules}


def test_a_doc_nao_cita_regra_de_reuniao_que_nao_existe():
    """A regra foi renomeada e a condição saiu; a doc ficou meio dia mentindo.

    `README.md` e o guia mostravam `reuniao_tarde` com `when=after("16:00")` depois
    de a função virar `reuniao` e a hora sair do gatilho. Ninguém percebeu porque
    nada comparava os dois — `test_toda_receita_do_guia_carrega` só prova que o
    bloco é Python válido, e um nome errado carrega perfeitamente.

    Só os nomes de `reuniao` são cobrados: as outras receitas são didáticas e não
    existem em `rules/` de propósito.
    """
    reais = regras_reais()
    citadas: set[str] = set()
    for doc in DOCS_COM_REGRAS:
        citadas |= set(re.findall(r"async def (\w+)\(ctx\)", doc.read_text()))

    fantasmas = {n for n in citadas if "reuniao" in n} - reais
    assert not fantasmas, f"a doc cita regra(s) que não existem: {sorted(fantasmas)}"


def test_toda_regra_real_aparece_no_guia():
    """O lado inverso: regra nova que ninguém documentou também é doc errada."""
    ausentes = {r for r in regras_reais() if r not in GUIA.read_text()}
    assert not ausentes, f"regra sem menção no guia: {sorted(ausentes)}"


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


PERFIL = "## Trabalho\nbackend de um sistema de pagamentos (Kafka, Postgres, Go)"


def test_o_perfil_de_priorities_entra_no_prompt_da_revisao():
    espiao = LLMEspiao()
    asyncio.run(
        espiao.review_capture(
            "revisar o consumer do Kafka",
            due=None,
            remind_at=None,
            priorities=PERFIL,
            contas="pessoal: gmail.com, trabalho: empresa.com",
        )
    )
    assert "sistema de pagamentos" in espiao.prompt
    assert "Kafka, Postgres, Go" in espiao.prompt


def test_apenas_o_dominio_da_conta_vai_para_o_modelo():
    """Domínio decide o roteamento; e-mail inteiro seria dado a mais."""
    espiao = LLMEspiao()
    asyncio.run(
        espiao.review_capture(
            "x", due=None, remind_at=None,
            contas="pessoal: gmail.com, trabalho: empresa.com",
        )
    )
    assert "empresa.com" in espiao.prompt
    assert "eu@empresa.com" not in espiao.prompt


def test_sem_perfil_o_prompt_nao_ganha_secao_vazia():
    espiao = LLMEspiao()
    asyncio.run(espiao.review_capture("x", due=None, remind_at=None))
    assert "Contexto de quem escreveu" not in espiao.prompt


def test_marcadores_do_cli_e_do_export_sao_os_mesmos():
    """Divergir faria a mesma nota parecer diferente em `ta list` e `ta export`."""
    from ta.cli import STATUS_MARK as cli_marks
    from ta.store import STATUS_MARK as export_marks

    assert cli_marks == export_marks


# ── O mural não reimplementa a ordem (ADR 0010) ─────────────────────────────
MURAL = Path(__file__).resolve().parents[1] / "src" / "ta" / "web" / "board.html"


def test_as_faixas_do_mural_e_do_python_sao_as_mesmas():
    """`HORIZON_LABEL` é o único lugar onde o JS nomeia as faixas.

    Sem este pino, renomear uma faixa no Python não quebra nada: a divisória
    simplesmente perde o rótulo, em silêncio.
    """
    from ta.store import HORIZONS

    corpo = re.search(r"const HORIZON_LABEL = \{(.*?)\};", MURAL.read_text(), re.S)
    assert corpo, "o mural perdeu o HORIZON_LABEL"
    assert set(re.findall(r"(\w+):", corpo[1])) == set(HORIZONS)


def test_o_mural_nao_reimplementa_a_ordem():
    """A ordem vem pronta de `GET /notes`; um sort no cliente é a regra em dobro."""
    assert "PRIO_RANK" not in MURAL.read_text()


def test_o_mural_avisa_quando_o_daemon_esta_velho():
    """Tirar a ordenação do cliente criou dependência da versão do servidor.

    O mural é servido com `no-store` e atualiza na hora; as rotas Python só depois
    de reiniciar. Contra um daemon velho não vem `horizon`, a ordem vem crua de
    `sort_key` e a tela fica **errada com cara de certa** — foi o que aconteceu de
    verdade. Sem harness de JS, este pino estático é o que impede a guarda de ser
    removida no próximo refactor.
    """
    corpo = MURAL.read_text()
    assert '"horizon" in todas[0]' in corpo, "o mural perdeu a guarda de versão"
    assert "systemctl --user restart ta" in corpo, "a guarda não diz o que fazer"


def test_o_aviso_fica_fora_do_quadro():
    """Na visão geral os post-its são `position: absolute` dentro do `#board`.

    A primeira versão do aviso foi inserida DENTRO dele e ficou ilegível atrás do
    primeiro post-it — visto na tela, não em teste. Fora do `#board` ele empurra o
    quadro para baixo em vez de ser coberto.
    """
    corpo = MURAL.read_text()
    assert corpo.index('id="aviso"') < corpo.index('id="board"')
