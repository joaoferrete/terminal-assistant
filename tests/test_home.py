"""The domain bug was silent: HA answered 200 and did nothing."""
from ta.actuators.home import Home


class FakeHome(Home):
    def __init__(self):
        super().__init__("http://x", "t")
        self.calls = []

    async def _request(self, method, path, payload=None):
        self.calls.append((path, payload))
        return {"state": "on"}


async def test_brightness_on_a_light_uses_the_light_domain():
    h = FakeHome()
    await h.switch_on("light.lampada_do_quarto", 70)
    path, payload = h.calls[0]
    assert path == "/api/services/light/turn_on"
    assert payload["brightness_pct"] == 70


async def test_brightness_on_a_switch_is_not_a_light_call():
    """The previous version called light/turn_on on a switch: 200, and nothing happened."""
    h = FakeHome()
    await h.switch_on("switch.ventilador_socket_1", 100)
    path, payload = h.calls[0]
    assert path == "/api/services/switch/turn_on"
    assert "brightness_pct" not in payload


async def test_zero_brightness_turns_off_in_either_domain():
    for entity, expected in (
        ("light.lampada_do_quarto", "/api/services/light/turn_off"),
        ("switch.ventilador_socket_1", "/api/services/switch/turn_off"),
    ):
        h = FakeHome()
        await h.switch_on(entity, 0)
        assert h.calls[0][0] == expected


async def test_with_no_brightness_it_turns_on_via_the_right_domain():
    h = FakeHome()
    await h.switch_on("switch.ventilador_socket_1")
    assert h.calls[0][0] == "/api/services/switch/turn_on"


async def test_confirm_reports_unconfirmed_without_blowing_up():
    """Reading the state right after the command returns the old value.

    Failing to confirm is not failing to command.
    """
    class Stuck(FakeHome):
        async def _request(self, method, path, payload=None):
            return {"state": "off"}

    h = Stuck()
    state, confirmed = await h.confirm("switch.x", "on", tries=2)
    assert (state, confirmed) == ("off", False)


# ── Resolving targets: groups and rooms ─────────────────────────────────────
from ta.config import resolve_targets  # noqa: E402

# Sample entities with Portuguese names on purpose: matching a room by a slice of
# its `friendly_name` has to survive accents and case, and `Lâmpada` is what
# proves it. They are fixture data, not anybody's house.
ENTITIES = [
    {"entity_id": "light.lampada_do_quarto", "attributes": {"friendly_name": "Lâmpada do quarto"}},
    {"entity_id": "light.teto_sala", "attributes": {"friendly_name": "Teto da sala"}},
    {
        "entity_id": "switch.ventilador_socket_1",
        "attributes": {"friendly_name": "Ventilador Socket 1", "device_class": "outlet"},
    },
    # A smart plug also exposes its child lock, which is a setting of the
    # appliance rather than an appliance. It has no `device_class` — that is how we
    # tell them apart.
    {
        "entity_id": "switch.ventilador_bloqueio_para_criancas",
        "attributes": {"friendly_name": "Ventilador Bloqueio para crianças"},
    },
]


def test_an_explicit_entity_id_passes_straight_through():
    assert resolve_targets("light.teto_sala", ENTITIES) == ["light.teto_sala"]


def test_the_luz_group_picks_up_every_light():
    assert resolve_targets("luz", ENTITIES) == ["light.lampada_do_quarto", "light.teto_sala"]
    assert resolve_targets("luzes", ENTITIES) == resolve_targets("luz", ENTITIES)


def test_the_tudo_group_includes_the_plug_but_not_the_child_lock():
    """`ta on tudo` used to switch on the lock: configuration is not an appliance."""
    assert resolve_targets("tudo", ENTITIES) == [
        "light.lampada_do_quarto",
        "light.teto_sala",
        "switch.ventilador_socket_1",
    ]


def test_a_room_matches_by_a_slice_of_the_name():
    assert resolve_targets("sala", ENTITIES) == ["light.teto_sala"]


def test_a_room_ignores_accents_and_case():
    """`Lâmpada do quarto` has to match `QUARTO`."""
    assert resolve_targets("QUARTO", ENTITIES) == ["light.lampada_do_quarto"]
    assert resolve_targets("lampada", ENTITIES) == ["light.lampada_do_quarto"]


def test_a_room_prefers_lights_and_only_falls_to_switches_if_there_are_none():
    """"turn on the bedroom" means the light. But "ventilador" is no light at all."""
    assert resolve_targets("ventilador", ENTITIES) == ["switch.ventilador_socket_1"]


def test_a_term_that_matches_nothing_returns_empty():
    assert resolve_targets("cozinha", ENTITIES) == []


def test_naming_it_explicitly_still_switches_the_lock_on():
    """A group is conservative; naming it explicitly is exact."""
    eid = "switch.ventilador_bloqueio_para_criancas"
    assert resolve_targets(eid, ENTITIES) == [eid]
