"""O registro que responde "o que funciona nesta máquina?".

Antes disto a resposta estava espalhada em cinco `shutil.which`, um
`Calendar.error` que o `/health` calculava e descartava, e um `Home.configured` —
e chegava ao usuário só pelo `journalctl`, depois de o daemon subir.

Estes testes cobrem as duas coisas que dão errado num diagnóstico: **mentir**
(dizer que falta o que está configurado) e **morrer** (estourar justamente no
ambiente pobre onde ele é mais necessário).
"""
from pathlib import Path

import pytest

from ta.capabilities import Capability, inspect, por_comando
from ta.cli import GRUPOS_DE_AJUDA, build_parser
from ta.config import Config

REPO = Path(__file__).resolve().parents[1]


def test_as_seis_capacidades_existem():
    chaves = [c.key for c in inspect(Config())]
    assert chaves == ["notes", "calendar", "mic", "home", "lighter", "ai"]


def test_so_as_notas_sao_essenciais():
    """É o posicionamento inteiro num assert: o núcleo roda em qualquer lugar.

    Se um dia outra integração for marcada essencial, `ta doctor` passa a sair com
    código != 0 em máquina que só quer capturar nota — e o projeto deixa de ser o
    que o README promete.
    """
    essenciais = [c.key for c in inspect(Config()) if c.essential]
    assert essenciais == ["notes"]


def test_toda_capacidade_indisponivel_diz_o_que_fazer():
    """Diagnóstico sem conserto só confirma o problema para quem já o viu."""
    for c in inspect(Config()):   # Config() vazia: quase tudo indisponível
        if not c.ok:
            assert c.fix, f"{c.key} não diz como consertar"


def test_a_ia_se_declara_opcional():
    """Sem isso, uma linha vermelha na tabela se lê como instalação quebrada."""
    ai = next(c for c in inspect(Config()) if c.key == "ai")
    assert not ai.ok
    assert "opcional" in ai.fix.lower()


def test_o_resumo_cabe_numa_linha():
    """O motivo da agenda traz o erro do GLib inteiro — certo no doctor, e errado
    na lista de comandos, onde empurra tudo para fora da tela."""
    c = Capability(key="x", label="X", ok=False, reason="curto. E aqui vem o resto longo.")
    assert c.resumo == "curto"


# ── O ambiente pobre, que é onde o diagnóstico mais importa ─────────────────
def test_a_agenda_degrada_sem_sessao_grafica(monkeypatch):
    """`GLib.GError: Cannot autolaunch D-Bus without X11 $DISPLAY`.

    Os typelibs existem, mas não há barramento — servidor, container, SSH sem
    sessão gráfica, e o CI. `_load` só capturava `ImportError` e `ValueError`,
    então esse erro ESTOURAVA e derrubava `ta doctor` e `ta --help` justamente
    onde eles precisam responder. Reproduzido com um erro que não é nenhum dos
    dois, que é o que a versão antiga deixava passar.
    """
    # Este teste precisa do `gi` PRESENTE, porque o cenário que ele reproduz é
    # "os typelibs existem e o barramento não". O job do núcleo do CI roda sem
    # `gi` nenhum de propósito, e lá não há o que reproduzir — quem exercita este
    # caminho é o job da agenda.
    gi = pytest.importorskip("gi", reason="o cenário exige typelibs instalados")

    from ta.sensors.calendar import Calendar

    class GErroDoGLib(Exception):
        pass

    cal = Calendar()

    def sem_barramento(*a, **k):
        raise GErroDoGLib("Cannot autolaunch D-Bus without X11 $DISPLAY (0)")

    # `SourceRegistry.new_sync` é o ponto exato onde o GLib estoura.
    gi.require_version("EDataServer", "1.2")
    from gi.repository import EDataServer

    monkeypatch.setattr(EDataServer.SourceRegistry, "new_sync", sem_barramento)

    assert cal.available is False, "a agenda deveria degradar, não estourar"
    assert "D-Bus" in cal.error
    assert "funciona sem ela" in cal.error, "o motivo não tranquiliza quem lê"


def test_uma_sonda_quebrada_nao_derruba_as_outras(monkeypatch):
    """Diagnóstico que morre no primeiro problema é inútil na máquina com problema.

    Foi a agenda que provou isso: sem sessão gráfica ela levava junto o `ta doctor`
    inteiro, e o comando existe justamente para ambientes assim.
    """
    from ta import capabilities

    def sonda_ruim(cfg):
        raise RuntimeError("hardware estranho")

    monkeypatch.setattr(capabilities, "SONDAS", (capabilities._notes, sonda_ruim))
    caps = capabilities.inspect(Config())

    assert len(caps) == 2
    assert caps[0].ok is True, "a sonda boa foi perdida junto"
    assert caps[1].ok is False
    assert "hardware estranho" in caps[1].reason


def test_o_help_sobrevive_a_uma_sonda_quebrada(monkeypatch):
    """E este é o que importa: `--help` que estoura é muito pior que sem marca."""
    from ta import capabilities

    monkeypatch.setattr(
        capabilities, "inspect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    epilogo = build_parser().epilog
    # Todos os grupos continuam listados, só sem a marca de disponibilidade.
    for _, titulo, comandos in GRUPOS_DE_AJUDA:
        assert titulo in epilogo
        assert comandos.split()[0] in epilogo


# ── O --help agrupado ───────────────────────────────────────────────────────
def test_todo_comando_do_parser_esta_num_grupo():
    """Um comando novo que ninguém agrupou some do `--help`, em silêncio.

    A lista plana do argparse foi removida de propósito, então o epílogo é a
    ÚNICA listagem — e o que não estiver nele não existe para quem lê.
    """
    registrados = set(build_parser()._subparsers._group_actions[0].choices)
    agrupados = {c for _, _, cmds in GRUPOS_DE_AJUDA for c in cmds.split()}
    assert registrados - agrupados == set(), "comando fora de todo grupo"


def test_os_comandos_agrupados_existem_de_verdade():
    """O inverso: grupo citando comando que não existe mais."""
    registrados = set(build_parser()._subparsers._group_actions[0].choices)
    agrupados = {c for _, _, cmds in GRUPOS_DE_AJUDA for c in cmds.split()}
    assert agrupados - registrados == set(), "grupo cita comando inexistente"


def test_luz_e_light_sao_o_mesmo_comando():
    escolhas = build_parser()._subparsers._group_actions[0].choices
    assert escolhas["luz"].get_default("func") is escolhas["light"].get_default("func")


def test_o_help_aponta_para_o_doctor():
    assert "ta doctor" in build_parser().epilog


def test_cada_comando_do_grupo_tem_capacidade_conhecida():
    """`por_comando` é o que ligaria um erro de runtime à sua capacidade."""
    mapa = por_comando(inspect(Config()))
    assert mapa["organize"].key == "ai"
    assert mapa["luz"].key == "home"
    assert mapa["note"].key == "notes"
