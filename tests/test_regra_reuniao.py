"""A regra que motivou o projeto, exercitada de verdade.

`ta rules check` só prova que ela **carrega**. Desde que a hora passou a decidir o
que a regra *faz* — e não *se* ela roda —, `16:00` governa três coisas ao mesmo
tempo: o ringlight na entrada, a luz na saída, e o texto do aviso. Nenhuma delas
era coberta.

Os dublês existem porque a regra fala com a casa e com a extensão: sem eles, o
teste acenderia a lâmpada do quarto e mexeria no `gsettings` do usuário.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ta.engine import Context, Trigger

# As Rules moram fora do pacote, em `rules/`, e são carregadas por caminho.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rules"))
import reuniao  # noqa: E402


class FakeHome:
    def __init__(self):
        self.brilhos = []

    async def light(self, entity, brilho=None):
        self.brilhos.append(brilho)


class FakeLighter:
    def __init__(self):
        self.acoes = []

    async def apply_profile(self, nome, *, enable=True):
        self.acoes.append(("profile", nome))

    async def enable(self, on=True):
        self.acoes.append(("enable", on))


class FakeCalendar:
    def __init__(self, titulo):
        self.titulo = titulo

    async def agora(self):
        return {"summary": self.titulo} if self.titulo else None


class FakeNotify:
    def __init__(self):
        self.enviadas = []   # (titulo, corpo) — o corpo importa: ele não pode mentir

    async def send(self, titulo, corpo, **k):
        self.enviadas.append((titulo, corpo))


async def _rodar(fn, *, hora, titulo, ativo=True):
    home, lighter, notify = FakeHome(), FakeLighter(), FakeNotify()
    h, m = (int(x) for x in hora.split(":"))
    await fn(
        Context(
            trigger=Trigger("mic", ativo),
            now=datetime(2026, 8, 11, h, m),
            home=home, lighter=lighter, notify=notify,
            calendar=FakeCalendar(titulo), note=None, extra={},
        )
    )
    return home, lighter, notify


# ── Entrada: a luz é incondicional, o ringlight não ─────────────────────────
@pytest.mark.parametrize("hora", ["09:00", "14:00", "17:00", "23:30"])
async def test_entrar_na_chamada_acende_a_luz_a_qualquer_hora(hora):
    """O pedido original amarrava isto às 16h. Numa call a luz ajuda sempre."""
    home, _, _ = await _rodar(reuniao.reuniao, hora=hora, titulo="Planning")
    assert home.brilhos == [100]


@pytest.mark.parametrize("hora", ["16:00", "17:00", "23:30"])
async def test_o_ringlight_entra_depois_que_escurece(hora):
    """`16:00` em ponto já conta como escuro: `after` é maior **ou igual**."""
    _, lighter, _ = await _rodar(reuniao.reuniao, hora=hora, titulo="Planning")
    assert ("profile", reuniao.PROFILE_RINGLIGHT) in lighter.acoes


@pytest.mark.parametrize("hora", ["09:00", "14:00", "15:59"])
async def test_de_dia_a_luz_acende_sozinha(hora):
    """A luz natural já dá conta; borda acesa de manhã é produção não pedida."""
    home, lighter, _ = await _rodar(reuniao.reuniao, hora=hora, titulo="Planning")
    assert home.brilhos == [100]
    assert lighter.acoes == []


@pytest.mark.parametrize(
    ("hora", "esperado"), [("09:00", "luz ligada"), ("17:00", "luz e ringlight ligados")]
)
async def test_o_aviso_nao_anuncia_ringlight_que_nao_ligou(hora, esperado):
    """Um aviso que mente uma vez deixa de ser lido nas outras."""
    _, _, notify = await _rodar(reuniao.reuniao, hora=hora, titulo="Planning")
    [(_, corpo)] = notify.enviadas
    assert corpo.endswith(esperado)


@pytest.mark.parametrize("hora", ["14:00", "17:00"])
async def test_um_a_um_nao_aciona_nada(hora):
    home, lighter, notify = await _rodar(reuniao.reuniao, hora=hora, titulo="1:1 com Thi")
    assert home.brilhos == []
    assert lighter.acoes == []
    assert notify.enviadas == []


# ── Fim: aqui a hora manda ──────────────────────────────────────────────────
async def test_sair_antes_das_16h_devolve_a_luz_ao_nivel_de_estar():
    home, lighter, _ = await _rodar(
        reuniao.fim_da_reuniao, hora="15:59", titulo="Planning", ativo=False
    )
    assert home.brilhos == [reuniao.NIVEL_DE_ESTAR]
    assert ("enable", False) in lighter.acoes


@pytest.mark.parametrize("hora", ["16:00", "16:30", "22:00"])
async def test_sair_depois_das_16h_deixa_a_luz_onde_esta(hora):
    """`16:00` em ponto já conta como tarde: `before` é estritamente menor.

    `16:30` é a reunião que atravessou as 16h — quem decide é a hora de sair.
    """
    home, lighter, _ = await _rodar(
        reuniao.fim_da_reuniao, hora=hora, titulo="Planning", ativo=False
    )
    assert home.brilhos == []               # a luz não foi tocada
    assert ("enable", False) in lighter.acoes   # mas o ringlight saiu


@pytest.mark.parametrize("hora", ["09:00", "15:59"])
async def test_o_ringlight_e_desligado_mesmo_tendo_comecado_de_dia(hora):
    """`enable(False)` é incondicional, e é de propósito.

    Repetir aqui a condição da entrada deixaria a borda acesa na reunião que
    começou às 15h50 e terminou às 16h10 — ligou (não), não desligou (sim). E
    desligar o que já está desligado não custa nada.
    """
    _, lighter, _ = await _rodar(
        reuniao.fim_da_reuniao, hora=hora, titulo="Planning", ativo=False
    )
    assert ("enable", False) in lighter.acoes


async def test_fim_de_um_a_um_nao_desfaz_o_que_nunca_foi_feito():
    home, lighter, _ = await _rodar(
        reuniao.fim_da_reuniao, hora="14:00", titulo="1:1 com Thi", ativo=False
    )
    assert home.brilhos == []
    assert lighter.acoes == []


async def test_sem_compromisso_na_agenda_o_fim_segue_o_caminho_comum():
    """Sair depois da hora marcada faz `calendar.now()` devolver nada.

    A regra ajusta assim mesmo: errar para o lado de ajustar é o lado barato.
    """
    home, _, _ = await _rodar(
        reuniao.fim_da_reuniao, hora="14:00", titulo="", ativo=False
    )
    assert home.brilhos == [reuniao.NIVEL_DE_ESTAR]
