from ta.sensors.mic import DEBOUNCE_S, MicWatcher, _streams_de_entrada


def node(media_class, app=None, node_name=None):
    props = {"media.class": media_class}
    if app:
        props["application.name"] = app
    if node_name:
        props["node.name"] = node_name
    return {"info": {"props": props}}


# ── Leitura do grafo ────────────────────────────────────────────────────────
def test_fora_de_call_nao_ha_stream_de_entrada():
    """O negativo verificado na máquina real: só fontes, nenhum stream."""
    dump = [node("Audio/Source", node_name="alsa_input.pci-0000_04_00.6"), node("Audio/Sink")]
    assert _streams_de_entrada(dump) == []


def test_reconhece_stream_de_captura():
    assert _streams_de_entrada([node("Stream/Input/Audio", app="Chromium")]) == ["Chromium"]


def test_ignora_o_monitor_do_proprio_gnome():
    """gnome-shell aparece como Stream/Input/Audio e não é reunião."""
    dump = [node("Stream/Input/Audio", app="gnome-shell"), node("Stream/Input/Audio", app="Zoom")]
    assert _streams_de_entrada(dump) == ["Zoom"]


def test_stream_de_saida_nao_conta():
    assert _streams_de_entrada([node("Stream/Output/Audio", app="Spotify")]) == []


# ── Debounce ────────────────────────────────────────────────────────────────
class FakeWatcher(MicWatcher):
    """Substitui a leitura do PipeWire por um roteiro."""

    def __init__(self, roteiro):
        self.eventos = []
        super().__init__(self._registrar)
        self._roteiro = list(roteiro)
        self._bin = "/fake/pw-dump"

    async def _registrar(self, ativo, apps):
        self.eventos.append((ativo, apps))

    async def _ler(self):
        return self._roteiro.pop(0) if self._roteiro else []


async def test_transicao_so_depois_do_debounce():
    w = FakeWatcher([["Zoom"], ["Zoom"]])
    await w.tick(0.0)                 # vê ativo, marca pendente
    assert w.eventos == []
    await w.tick(DEBOUNCE_S + 0.1)     # confirma
    assert w.eventos == [(True, ["Zoom"])]
    assert w.active


async def test_rajada_curta_nao_dispara():
    """Stream que abre e fecha em rajada não deve piscar a luz."""
    w = FakeWatcher([["Zoom"], []])
    await w.tick(0.0)
    await w.tick(0.5)                  # voltou a vazio antes do debounce
    assert w.eventos == []
    assert not w.active


async def test_transicao_nos_dois_sentidos():
    w = FakeWatcher([["Zoom"], ["Zoom"], [], []])
    await w.tick(0.0)
    await w.tick(DEBOUNCE_S + 0.1)
    await w.tick(100.0)
    await w.tick(100.0 + DEBOUNCE_S + 0.1)
    assert w.eventos == [(True, ["Zoom"]), (False, [])]
    assert not w.active


class FalhaNaLeitura(FakeWatcher):
    async def _ler(self):
        return None


async def test_falha_de_leitura_nao_inventa_transicao():
    """None é 'não consegui ler', diferente de 'nada rodando'."""
    w = FalhaNaLeitura([])
    w.active = True
    await w.tick(0.0)
    await w.tick(100.0)
    assert w.eventos == []
    assert w.active     # mantém o estado, não conclui que a call caiu
