"""Apelidos, grupos e regras deixaram de ser código-fonte (ADR 0014).

Enquanto o repositório foi de uma pessoa, `quarto = light.lampada_do_quarto` em
`config.py` era prático. Aberto, significaria que configurar a própria casa exige
editar o pacote instalado — e a edição some na próxima reinstalação, enquanto as
regras dentro do checkout conflitam em todo `git pull`.

O que estes testes protegem, além do caminho novo, é a **compatibilidade**: quem
já tinha regras em `<repo>/rules` não pode perdê-las por causa desta mudança.
"""
from pathlib import Path

import pytest

from ta import config as cfg_mod
from ta.config import config_file, entity_aliases, groups, resolve_entity, resolve_targets

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def config_isolada(tmp_path, monkeypatch):
    """Nenhum teste pode ler — nem escrever — a config real do usuário.

    O `lru_cache` de `_user_config` sobrevive entre testes, então limpar o cache
    faz parte do isolamento: sem isso, o primeiro teste que lê fixa o resultado
    para todos os outros.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    yield
    cfg_mod._user_config.cache_clear()


def escrever(tmp_path, texto: str) -> None:
    d = tmp_path / "ta"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(texto)
    cfg_mod._user_config.cache_clear()


# ── Sem arquivo nenhum ──────────────────────────────────────────────────────
def test_sem_config_o_projeto_funciona():
    """Uma casa sem apelidos não é uma instalação quebrada.

    É o estado de todo mundo que clonar, e tem que ser um estado utilizável.
    """
    assert entity_aliases() == {}
    assert resolve_entity("light.qualquer") == "light.qualquer"
    # Termo desconhecido volta como veio, para o passo de "ambiente por trecho do
    # nome" ainda ter chance de casar contra o inventário do HA.
    assert resolve_entity("quarto") == "quarto"


def test_os_grupos_embutidos_existem_sem_config():
    """Grupo por domínio não é pessoal — vale para qualquer casa."""
    assert set(groups()) == {"luz", "luzes", "tomada", "tomadas", "tudo"}
    assert groups()["tudo"] == ("light.", "switch.")


# ── Com arquivo ─────────────────────────────────────────────────────────────
def test_apelido_do_arquivo_e_usado(tmp_path):
    escrever(tmp_path, '[aliases]\nquarto = "light.lampada"\n')
    assert resolve_entity("quarto") == "light.lampada"


def test_grupo_do_usuario_soma_aos_embutidos(tmp_path):
    escrever(tmp_path, '[groups]\nescritorio = ["light.", "switch."]\n')
    assert groups()["escritorio"] == ("light.", "switch.")
    assert "tudo" in groups(), "o grupo do usuário não pode apagar os embutidos"


def test_grupo_do_usuario_vence_no_conflito(tmp_path):
    escrever(tmp_path, '[groups]\ntudo = ["light."]\n')
    assert groups()["tudo"] == ("light.",)


def test_grupo_tem_precedencia_sobre_apelido(tmp_path):
    """A ordem de resolução é do mais específico ao mais amplo, e não pode virar.

    Um apelido chamado `luz` não pode sequestrar o grupo `luz`.
    """
    escrever(tmp_path, '[aliases]\nluz = "light.especifica"\n')
    entities = [
        {"entity_id": "light.a", "attributes": {}},
        {"entity_id": "light.b", "attributes": {}},
    ]
    assert resolve_targets("luz", entities) == ["light.a", "light.b"]


# ── Arquivo quebrado ────────────────────────────────────────────────────────
def test_toml_invalido_nao_derruba_o_daemon(tmp_path, caplog):
    """Falhar por arquivo de conveniência é desproporcional. Falhar CALADO é pior.

    O daemon segue de pé sem apelidos, e o motivo vai para o log — senão o sintoma
    seria `ta on quarto` parando de funcionar sem explicação nenhuma.
    """
    escrever(tmp_path, "[aliases\nquarto = quebrado")
    with caplog.at_level("WARNING"):
        assert entity_aliases() == {}
    assert "config.toml" in caplog.text


@pytest.mark.parametrize("corpo", ['aliases = "não é tabela"', "[aliases]\n"])
def test_formato_inesperado_degrada_para_vazio(tmp_path, corpo):
    escrever(tmp_path, corpo)
    assert entity_aliases() == {}


# ── XDG ─────────────────────────────────────────────────────────────────────
def test_respeita_xdg_config_home(tmp_path):
    """Mesmo padrão que `db.default_db_path()` já usava — ele estava certo."""
    assert config_file() == tmp_path / "ta" / "config.toml"


# ── Nada pessoal sobrou no código ───────────────────────────────────────────
def test_o_codigo_nao_carrega_o_inventario_de_ninguem():
    """O ponto inteiro do ADR 0014, num assert.

    Se alguém reintroduzir um `entity_id` concreto em `config.py`, é aqui que
    aparece — e o modo de falha que isso causa (editar o pacote instalado, perder
    na reinstalação) é invisível até acontecer com outra pessoa.
    """
    # O `src/` INTEIRO, não só `config.py`. A primeira versão deste teste olhava
    # um arquivo só, e por isso não viu os oito `entity_id` fixos em
    # `actuators/home.py` que alimentavam `ta temp` e `ta router` — para qualquer
    # casa que não a do autor, os dois comandos devolviam nulo em silêncio.
    suspeitos = []
    for arq in (REPO / "src").rglob("*.py"):
        corpo = arq.read_text()
        for marca in ("lampada_do_quarto", "ventilador_socket", "ventilador_energia",
                      "forecast_casa", "sensor.s7_"):
            if marca in corpo:
                suspeitos.append(f"{arq.relative_to(REPO)}: {marca}")
    assert not suspeitos, f"entity_id de uma casa específica no código: {suspeitos}"


def test_as_regras_de_exemplo_nao_sao_carregadas_no_boot():
    """`examples/rules/` é documentação versionada, não regra ativa.

    Carregada no boot de um estranho, `reuniao.py` miraria uma lâmpada que não
    existe na casa dele — e o motor não tem como saber a diferença entre isso e
    uma regra correta.
    """
    from ta.daemon import EXAMPLE_RULES, user_rules_dir

    assert EXAMPLE_RULES.name == "rules"
    assert EXAMPLE_RULES.parent.name == "examples"
    assert user_rules_dir() != EXAMPLE_RULES
