"""O bug do domínio era silencioso: o HA respondia 200 e não fazia nada."""
from ta.actuators.home import Home


class FakeHome(Home):
    def __init__(self):
        super().__init__("http://x", "t")
        self.chamadas = []

    async def _request(self, method, path, payload=None):
        self.chamadas.append((path, payload))
        return {"state": "on"}


async def test_brilho_em_light_usa_o_dominio_light():
    h = FakeHome()
    await h.switch_on("light.lampada_do_quarto", 70)
    path, payload = h.chamadas[0]
    assert path == "/api/services/light/turn_on"
    assert payload["brightness_pct"] == 70


async def test_brilho_em_switch_nao_vira_chamada_de_light():
    """A versão anterior chamava light/turn_on num switch: 200 e nada acontecia."""
    h = FakeHome()
    await h.switch_on("switch.ventilador_socket_1", 100)
    path, payload = h.chamadas[0]
    assert path == "/api/services/switch/turn_on"
    assert "brightness_pct" not in payload


async def test_brilho_zero_apaga_em_qualquer_dominio():
    for entity, esperado in (
        ("light.lampada_do_quarto", "/api/services/light/turn_off"),
        ("switch.ventilador_socket_1", "/api/services/switch/turn_off"),
    ):
        h = FakeHome()
        await h.switch_on(entity, 0)
        assert h.chamadas[0][0] == esperado


async def test_sem_brilho_liga_pelo_dominio_certo():
    h = FakeHome()
    await h.switch_on("switch.ventilador_socket_1")
    assert h.chamadas[0][0] == "/api/services/switch/turn_on"


async def test_confirm_devolve_nao_confirmado_sem_estourar():
    """Ler o estado logo após o comando devolve o valor antigo. Falhar em
    confirmar não é falhar em comandar."""
    class Travado(FakeHome):
        async def _request(self, method, path, payload=None):
            return {"state": "off"}

    h = Travado()
    estado, confirmado = await h.confirm("switch.x", "on", tries=2)
    assert (estado, confirmado) == ("off", False)


# ── Resolução de alvos: grupo e ambiente ────────────────────────────────────
from ta.config import resolve_targets  # noqa: E402

ENTIDADES = [
    {"entity_id": "light.lampada_do_quarto", "attributes": {"friendly_name": "Lâmpada do quarto"}},
    {"entity_id": "light.teto_sala", "attributes": {"friendly_name": "Teto da sala"}},
    {
        "entity_id": "switch.ventilador_socket_1",
        "attributes": {"friendly_name": "Ventilador Socket 1", "device_class": "outlet"},
    },
    # A tomada Ekasa expõe também o travamento infantil, que é ajuste do aparelho
    # e não aparelho. Ele não tem `device_class` — é assim que o distinguimos.
    {
        "entity_id": "switch.ventilador_bloqueio_para_criancas",
        "attributes": {"friendly_name": "Ventilador Bloqueio para crianças"},
    },
]


def test_entity_id_explicito_passa_direto():
    assert resolve_targets("light.teto_sala", ENTIDADES) == ["light.teto_sala"]


def test_grupo_luz_pega_todas_as_luzes():
    assert resolve_targets("luz", ENTIDADES) == ["light.lampada_do_quarto", "light.teto_sala"]
    assert resolve_targets("luzes", ENTIDADES) == resolve_targets("luz", ENTIDADES)


def test_grupo_tudo_inclui_a_tomada_mas_nao_o_travamento_infantil():
    """`ta on tudo` ligava a trava: configuração não é aparelho."""
    assert resolve_targets("tudo", ENTIDADES) == [
        "light.lampada_do_quarto",
        "light.teto_sala",
        "switch.ventilador_socket_1",
    ]


def test_ambiente_casa_por_trecho_do_nome():
    assert resolve_targets("sala", ENTIDADES) == ["light.teto_sala"]


def test_ambiente_ignora_acento_e_caso():
    """`Lâmpada do quarto` tem que casar com `QUARTO`."""
    assert resolve_targets("QUARTO", ENTIDADES) == ["light.lampada_do_quarto"]
    assert resolve_targets("lampada", ENTIDADES) == ["light.lampada_do_quarto"]


def test_ambiente_prefere_luz_e_so_cai_pra_switch_se_nao_houver():
    """'ligar o quarto' quer dizer a luz. Mas 'ventilador' não é luz nenhuma."""
    assert resolve_targets("ventilador", ENTIDADES) == ["switch.ventilador_socket_1"]


def test_termo_sem_correspondencia_devolve_vazio():
    assert resolve_targets("cozinha", ENTIDADES) == []


def test_nomear_explicitamente_ainda_liga_a_trava():
    """Grupo é conservador; explícito é exato."""
    eid = "switch.ventilador_bloqueio_para_criancas"
    assert resolve_targets(eid, ENTIDADES) == [eid]
