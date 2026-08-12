"""Aliases, groups and rules stopped being source code (ADR 0014).

While the repository belonged to one person, `quarto = light.lampada_do_quarto` in
`config.py` was practical. Opened up, it would mean configuring your own house
requires editing the installed package — and that edit disappears on the next
reinstall, while rules inside the checkout conflict on every `git pull`.

What these tests protect, besides the new path, is **compatibility**: anybody who
already had rules in `<repo>/rules` must not lose them to this change.
"""
from pathlib import Path

import pytest

from ta import config as cfg_mod
from ta.config import config_file, entity_aliases, groups, resolve_entity, resolve_targets

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No test may read — or write — the user's real config.

    `_user_config`'s `lru_cache` survives between tests, so clearing it is part of
    the isolation: without that, the first test that reads pins the result for
    every other one.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    yield
    cfg_mod._user_config.cache_clear()


def write(tmp_path, text: str) -> None:
    d = tmp_path / "ta"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(text)
    cfg_mod._user_config.cache_clear()


# ── With no file at all ─────────────────────────────────────────────────────
def test_the_project_works_with_no_config():
    """A house with no aliases is not a broken installation.

    It is the state of everybody who clones, and it has to be a usable one.
    """
    assert entity_aliases() == {}
    assert resolve_entity("light.qualquer") == "light.qualquer"
    # An unknown term comes back as it arrived, so the "room by a slice of the
    # name" step still gets a chance to match against HA's own inventory.
    assert resolve_entity("quarto") == "quarto"


def test_the_builtin_groups_exist_without_a_config():
    """A group by domain is not personal — it holds for any house."""
    assert set(groups()) == {"luz", "luzes", "tomada", "tomadas", "tudo"}
    assert groups()["tudo"] == ("light.", "switch.")


# ── With a file ─────────────────────────────────────────────────────────────
def test_apelido_do_file_uivo_e_usado(tmp_path):
    write(tmp_path, '[aliases]\nquarto = "light.lampada"\n')
    assert resolve_entity("quarto") == "light.lampada"


def test_a_user_group_adds_to_the_builtin_ones(tmp_path):
    write(tmp_path, '[groups]\nescritorio = ["light.", "switch."]\n')
    assert groups()["escritorio"] == ("light.", "switch.")
    assert "tudo" in groups(), "a user group must not erase the built-in ones"


def test_a_user_group_wins_a_conflict(tmp_path):
    write(tmp_path, '[groups]\ntudo = ["light."]\n')
    assert groups()["tudo"] == ("light.",)


def test_a_group_takes_precedence_over_an_alias(tmp_path):
    """Resolution goes from most specific to broadest, and must not flip.

    An alias called `luz` must not hijack the `luz` group.
    """
    write(tmp_path, '[aliases]\nluz = "light.especifica"\n')
    entities = [
        {"entity_id": "light.a", "attributes": {}},
        {"entity_id": "light.b", "attributes": {}},
    ]
    assert resolve_targets("luz", entities) == ["light.a", "light.b"]


# ── A broken file ───────────────────────────────────────────────────────────
def test_invalid_toml_does_not_take_the_daemon_down(tmp_path, caplog):
    """Failing over a convenience file is disproportionate. Failing SILENTLY is worse.

    The daemon stays up without aliases, and the reason goes to the log —
    otherwise the symptom would be `ta on quarto` quietly stopping to work with no
    explanation at all.
    """
    write(tmp_path, "[aliases\nquarto = broken")
    with caplog.at_level("WARNING"):
        assert entity_aliases() == {}
    assert "config.toml" in caplog.text


@pytest.mark.parametrize("body", ['aliases = "not a table"', "[aliases]\n"])
def test_an_unexpected_shape_degrades_to_empty(tmp_path, body):
    write(tmp_path, body)
    assert entity_aliases() == {}


# ── XDG ─────────────────────────────────────────────────────────────────────
def test_it_respects_xdg_config_home(tmp_path):
    """The same pattern `db.default_db_path()` already used — it was right."""
    assert config_file() == tmp_path / "ta" / "config.toml"


# ── Nothing personal left in the code ───────────────────────────────────────
def test_the_code_carries_nobodys_inventory():
    """The whole point of ADR 0014, in one assertion.

    If somebody reintroduces a concrete `entity_id` into `config.py`, this is
    where it shows up — and the failure mode it causes (editing the installed
    package, losing it on reinstall) is invisible until it happens to somebody
    else.
    """
    # The WHOLE of `src/`, not just `config.py`. The first version of this test
    # looked at one file, which is why it missed the eight hardcoded `entity_id`s
    # in `actuators/home.py` that fed `ta temp` and `ta router` — for any house
    # but the author's, both commands returned nulls in silence.
    suspects = []
    for file_ in (REPO / "src").rglob("*.py"):
        body = file_.read_text()
        for mark in ("lampada_do_quarto", "ventilador_socket", "ventilador_energia",
                      "forecast_casa", "sensor.s7_"):
            if mark in body:
                suspects.append(f"{file_.relative_to(REPO)}: {mark}")
    assert not suspects, f"entity_id from one specific house in the code: {suspects}"


def test_the_example_rules_are_not_loaded_at_boot():
    """`examples/rules/` is versioned documentation, not an active rule.

    Loaded at a stranger's boot, `meeting.py` would aim at a lamp that does not
    exist in their house — and the engine has no way to tell that apart from a
    correct rule.
    """
    from ta.daemon import EXAMPLE_RULES, user_rules_dir

    assert EXAMPLE_RULES.name == "rules"
    assert EXAMPLE_RULES.parent.name == "examples"
    assert user_rules_dir() != EXAMPLE_RULES
