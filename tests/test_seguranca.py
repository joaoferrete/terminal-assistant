"""O daemon expõe 29 rotas e não tem autenticação própria.

Enquanto isto foi um assistente de uma máquina só, `0.0.0.0` era escolha
consciente e documentada — o mural abre no celular. Publicado, o mesmo padrão
entrega as notas inteiras, o comando da casa e a chave do modelo para qualquer
vizinho de wifi de quem seguir o passo a passo.

Estes testes fixam as duas metades da decisão (ADR 0012): o padrão é loopback, e
sair dele exige credencial. São testes de padrão, e é justamente por isso que
existem — um padrão errado não dá erro, ele funciona.
"""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

# Sem `__init__.py` em `tests/`, o pytest põe o diretório no path e o import é
# direto. Os dublês são os mesmos do `test_daemon`: teste não fala com o
# Evolution Data Server nem com o `gsettings` do usuário.
from test_daemon import FakeCalendar, FakeLighter

from ta.config import DEFAULT_HOST, Config, ConfigError
from ta.daemon import create_app

TOKEN = "token-de-teste-nao-e-segredo"
REPO = Path(__file__).resolve().parents[1]
MURAL = REPO / "src" / "ta" / "web" / "board.html"


def montar(tmp_path, **kw):
    return create_app(
        Config(ha_token="fake", gemini_api_key="fake", auto_review=False, **kw),
        db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "sem-regras",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )


# ── O padrão ────────────────────────────────────────────────────────────────
def test_o_padrao_e_loopback():
    """Se alguém trocar isto de volta, o teste é o que grita.

    Não é preferência: é a diferença entre expor as notas de quem instalou para a
    rede dele ou não.
    """
    assert DEFAULT_HOST == "127.0.0.1"
    assert not Config().exposed


def test_loopback_nao_exige_token():
    """Uso local segue exatamente como era. A mudança não pode custar rotina."""
    Config().check()   # não levanta
    assert Config().token is None


# ── Sair do loopback ────────────────────────────────────────────────────────
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "::"])
def test_bind_exposto_sem_token_recusa_subir(host, tmp_path):
    """Falha alto e cedo, não depois — ninguém relê log de boot."""
    with pytest.raises(ConfigError) as e:
        montar(tmp_path, host=host)

    # A mensagem tem que dizer o conserto, não só o problema.
    assert "TA_TOKEN" in str(e.value)
    assert "systemctl --user restart ta" in str(e.value)


def test_bind_exposto_com_token_sobe(tmp_path):
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200


# ── O middleware ────────────────────────────────────────────────────────────
# O `TestClient` se apresenta como `testclient`, que não é loopback — então ele
# cai no caminho do estranho sem precisar de encenação. Para exercitar o caminho
# local, o par é declarado explicitamente.
def test_estranho_sem_credencial_leva_401(tmp_path):
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/notes")
        assert r.status_code == 401
        assert "TA_TOKEN" in r.json()["error"]


def test_estranho_com_token_errado_leva_401(tmp_path):
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/notes", headers={"Authorization": "Bearer quase-certo"})
        assert r.status_code == 401


def test_o_token_tambem_vale_na_query(tmp_path):
    """É como o mural boota: `/board?token=…` carrega o HTML, e daí o JS assume."""
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        assert c.get(f"/board?token={TOKEN}").status_code == 200
        assert c.get("/board").status_code == 401


def test_cliente_local_passa_sem_credencial_mesmo_com_o_daemon_aberto(tmp_path):
    """`ta note` na própria máquina não deve carregar segredo.

    Quem está aqui já tem o `.env`; exigir token dele não compra segurança, só
    atrito. O risco que o middleware cobre é a rede.
    """
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("127.0.0.1", 51000)) as c:
        assert c.get("/notes").status_code == 200


def test_par_ipv4_mapeado_conta_como_local(tmp_path):
    """Socket IPv6 aceitando IPv4 reporta `::ffff:127.0.0.1`.

    Sem tratar isso, o CLI local passaria a precisar de token dependendo da
    família do socket — falha que só apareceria na máquina de outra pessoa.
    """
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("::ffff:127.0.0.1", 51000)) as c:
        assert c.get("/notes").status_code == 200


# ── O mural ─────────────────────────────────────────────────────────────────
# Não há harness de JS aqui, então o pino é estático — mesmo padrão que o
# `test_docs.py` usa para as faixas de horizonte. Serve para a guarda não sumir
# num refactor: sem ela o mural no celular carrega o HTML e todas as chamadas
# dão 401, o que se lê como "o mural quebrou".
def test_o_mural_manda_a_credencial():
    corpo = MURAL.read_text()
    assert "Bearer ${token}" in corpo, "o mural não manda o token nas chamadas"
    assert "searchParams.get(\"token\")" in corpo, "o mural não boota pelo ?token="


def test_o_mural_tira_o_token_da_barra_de_endereco():
    """Deixar o segredo na URL o joga no histórico e em todo link colado."""
    corpo = MURAL.read_text()
    assert "searchParams.delete(\"token\")" in corpo
    assert "history.replaceState" in corpo


# ── O que o /health pode contar ─────────────────────────────────────────────
def test_health_nunca_devolve_valor_de_segredo(tmp_path):
    """Ele reporta PRESENÇA de segredo. A disciplina é antiga e não pode regredir."""
    with TestClient(montar(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("127.0.0.1", 51000)) as c:
        corpo = c.get("/health").text

    for segredo in (TOKEN, "fake"):
        assert segredo not in corpo


# ── Nada de segredo pode entrar no repositório ──────────────────────────────
def test_o_gitignore_cobre_backups_do_env():
    """`.env` estava coberto; `.env.bak-*` não estava.

    Um backup do `.env` feito antes de editá-lo, mais um `git add -A`, levou
    HA_TOKEN e GEMINI_API_KEY reais para o histórico. Foi pego pela varredura de
    publicação e expurgado antes de qualquer push, mas o buraco era do
    `.gitignore` e é lá que ele se fecha.

    Cobrir só `.env` protege o arquivo e não protege as cópias dele, que é o que
    alguém cria justamente quando vai mexer em segredo.
    """
    padroes = (REPO / ".gitignore").read_text()
    assert ".env" in padroes
    assert ".env.bak*" in padroes, "backup do .env não está coberto"


def test_nenhum_arquivo_de_segredo_esta_rastreado():
    """A checagem direta, para não depender de ninguém lembrar de varrer.

    `.env.example` é a única exceção, e existe justamente para ser versionado:
    ele tem os nomes das chaves e nenhum valor.
    """
    import subprocess

    saida = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    suspeitos = [f for f in saida if f.startswith(".env") and f != ".env.example"]
    assert not suspeitos, f"arquivo de segredo rastreado: {suspeitos}"
