"""`TA_LANG` governa parser, texto e o idioma em que o modelo escreve (ADR 0013).

Os testes de parser em inglês existem porque as formas são **estruturalmente**
diferentes, não é troca de palavras: português diz "17 de outubro" e "na sexta",
inglês diz "October 17" e "next Friday" — e AM/PM, que português não precisa e
inglês não vive sem.

O teste que mais importa é o de `@03/04`: ele fixa por que a decisão foi "um
idioma por vez" em vez de "os dois juntos".
"""
from datetime import date, datetime

import pytest

from ta import i18n
from ta.notes import parse

# Uma terça-feira, para "next friday" e "@sexta" terem resposta previsível.
AGORA = datetime(2026, 8, 11, 9, 0)


@pytest.fixture
def em(monkeypatch):
    """Fixa o idioma e limpa os caches dos dois lados."""

    def _fixar(lang: str):
        monkeypatch.setenv("TA_LANG", lang)
        i18n.reset_cache()
        return lambda texto: parse(texto, now=AGORA)

    yield _fixar
    i18n.reset_cache()


# ── Precedência ─────────────────────────────────────────────────────────────
def test_ta_lang_vence_tudo(monkeypatch):
    monkeypatch.setenv("TA_LANG", "en")
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    i18n.reset_cache()
    assert i18n.lang() == "en"
    assert i18n.lang_source() == "TA_LANG"


def test_o_locale_do_sistema_vale_quando_nada_foi_configurado(monkeypatch, tmp_path):
    """É o que faz nada mudar para quem já usa: numa máquina pt_BR, segue em pt.

    E é por isso que a primeira execução não precisa perguntar o idioma — o que
    também evitaria pôr a pergunta no `ta init`, que exige LLM.
    """
    monkeypatch.delenv("TA_LANG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    from ta import config

    config._user_config.cache_clear()
    i18n.reset_cache()
    assert i18n.lang() == "pt"
    assert i18n.lang_source() == "system locale"


def test_locale_nao_suportado_cai_no_padrao(monkeypatch, tmp_path):
    """`de_DE` não vira alemão por adivinhação — vira inglês, e é dito."""
    monkeypatch.delenv("TA_LANG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    from ta import config

    config._user_config.cache_clear()
    i18n.reset_cache()
    assert i18n.lang() == "en"


def test_ta_lang_invalido_avisa(monkeypatch, caplog, tmp_path):
    """Quem escreveu `TA_LANG=de` fez uma escolha; ignorá-la calado engana."""
    monkeypatch.setenv("TA_LANG", "de")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    i18n.reset_cache()
    with caplog.at_level("WARNING"):
        assert i18n.lang() == "en"
    assert "TA_LANG" in caplog.text


# ── O catálogo não pode divergir ────────────────────────────────────────────
def test_os_dois_idiomas_tem_as_mesmas_chaves():
    """Sem este pino, uma chave só em `pt` vira texto errado para quem usa `en`.

    O mesmo defeito que o `HORIZON_LABEL` do mural já tinha: uma tabela paralela
    que ninguém compara com a outra.
    """
    por_lang = {
        lang: {k for k, v in i18n.MESSAGES.items() if lang in v}
        for lang in i18n.LANGS
    }
    assert por_lang["pt"] == por_lang["en"]


def test_chave_desconhecida_nao_estoura():
    """Mensagem feia é melhor que KeyError no meio de um comando que ia funcionar."""
    assert i18n.t("nao.existe") == "nao.existe"


# ── A ambiguidade que motivou a decisão ─────────────────────────────────────
def test_a_data_numerica_segue_o_idioma(em):
    """`@03/04` é 3 de abril em pt e 4 de março em en. É o ADR 0013 num assert.

    Aceitar os dois vocabulários ao mesmo tempo faria este token devolver uma data
    VÁLIDA E ERRADA para metade dos usuários — sem erro, sem marca faltando.
    """
    assert em("pt")("reunião @03/04").due == date(2027, 4, 3)
    assert em("en")("meeting @03/04").due == date(2027, 3, 4)


# ── Parser em inglês ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("call the dentist @friday", date(2026, 8, 14)),
        ("call the dentist @fri", date(2026, 8, 14)),
        ("pay rent @tomorrow", date(2026, 8, 12)),
        ("standup @today", date(2026, 8, 11)),
        ("dentist on October 17", date(2026, 10, 17)),
        ("dentist 17 October", date(2026, 10, 17)),
        ("dentist October 17th", date(2026, 10, 17)),
        ("review next friday", date(2026, 8, 14)),
        ("review on monday", date(2026, 8, 17)),
    ],
)
def test_datas_em_ingles(em, texto, esperado):
    assert em("en")(texto).due == esperado


@pytest.mark.parametrize(
    ("texto", "hora"),
    [
        ("meeting tomorrow at 8", 8),
        ("meeting tomorrow at 8am", 8),
        ("meeting tomorrow at 8pm", 20),
        ("meeting tomorrow at 12pm", 12),
        ("meeting tomorrow at 12am", 0),
    ],
)
def test_am_pm(em, texto, hora):
    """AM/PM não existia no parser: `8pm` não casava NADA, em silêncio.

    O lookahead `(?!\\S)` falhava no `p` e a marca era descartada sem aviso.
    """
    n = em("en")(texto)
    assert n.remind_at is not None, "o horário não foi reconhecido"
    assert n.remind_at.hour == hora


def test_retrospectivo_em_ingles_nao_vira_prazo(em):
    """"today I learned X" é registro, não tarefa. O regex acerta a forma e
    erraria a intenção sem a lista curada."""
    n = em("en")("today I learned about WAL mode")
    assert n.due is None
    assert n.is_task is False


def test_numero_solto_em_ingles_nao_vira_lembrete(em):
    """"8 hours of battery" é o falso positivo mais provável de todos."""
    assert em("en")("laptop runs 8h of battery").remind_at is None


def test_prioridade_em_ingles(em):
    assert em("en")("ship the thing !high").priority == "high"


# ── O português não regrediu ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("ligar dentista @sexta", date(2026, 8, 14)),
        ("nutricionista 17 de setembro", date(2026, 9, 17)),
        ("revisar na quinta", date(2026, 8, 13)),
        ("pagar aluguel @amanha", date(2026, 8, 12)),
    ],
)
def test_datas_em_portugues_seguem_iguais(em, texto, esperado):
    assert em("pt")(texto).due == esperado


def test_retrospectivo_em_portugues_segue_igual(em):
    assert em("pt")("hoje aprendi sobre WAL").due is None


# ── O mural recebe o catálogo pronto ────────────────────────────────────────
def _mural(monkeypatch, tmp_path, lang):
    from starlette.testclient import TestClient
    from test_daemon import FakeCalendar, FakeLighter

    from ta.config import Config
    from ta.daemon import create_app

    monkeypatch.setenv("TA_LANG", lang)
    i18n.reset_cache()
    app = create_app(
        Config(auto_review=False), db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "sem-regras", calendar=FakeCalendar(),
        lighter=FakeLighter(), background=False,
    )
    with TestClient(app) as c:
        return c.get("/board").text


def test_o_catalogo_e_injetado_no_html(monkeypatch, tmp_path):
    """Injetado, e não buscado por uma rota: o idioma não muda durante a página.

    Uma segunda requisição só acrescentaria latência e um instante desenhando
    chaves cruas antes de o catálogo chegar.
    """
    html = _mural(monkeypatch, tmp_path, "pt")
    assert "/*__I18N__*/{}" not in html, "o marcador não foi substituído"
    assert "próximos 7 dias" in html


def test_o_mural_muda_de_idioma(monkeypatch, tmp_path):
    assert "próximos 7 dias" in _mural(monkeypatch, tmp_path, "pt")
    assert "next 7 days" in _mural(monkeypatch, tmp_path, "en")


def test_o_marcador_sobrevive_no_arquivo_em_disco():
    """O arquivo tem que continuar abrindo direto no navegador durante o dev.

    Por isso o marcador é um comentário JS seguido de `{}`, e não um placeholder
    que deixaria o arquivo com sintaxe inválida fora do daemon.
    """
    from ta.daemon import BOARD_HTML, I18N_MARKER

    assert I18N_MARKER in BOARD_HTML.read_text()


def test_o_dia_do_mes_nao_vira_hora(em):
    """"dentist on October 17" ganhava lembrete às 17:00.

    A data já tinha sido achada, então a preposição deixava de ser exigida, e o
    `17` do dia era relido como hora. Português não sofria disso porque `8h` e
    `8:30` exigem `h` ou `:`; o inglês precisa da regra escrita, porque `8pm`
    obriga o sufixo a ser opcional no regex.
    """
    n = em("en")("dentist on October 17")
    assert n.due == date(2026, 10, 17)
    assert n.remind_at is None, "o dia do mês virou horário"


def test_hora_de_verdade_junto_da_data_ainda_conta(em):
    """A guarda não pode custar o caso legítimo."""
    n = em("en")("dentist on October 17 at 8:30")
    assert n.due == date(2026, 10, 17)
    assert n.remind_at.hour == 8 and n.remind_at.minute == 30


def test_a_suite_nao_depende_do_locale_da_maquina():
    """Nenhum teste pode depender do idioma de quem o roda.

    Os 17 testes de parser em português passavam na máquina do autor
    (`pt_BR.UTF-8`) e falhavam no CI, que não define `LANG` — o padrão vira `en` e
    `@sexta` deixa de ser data. Foi o CI que pegou, na primeira execução.

    Este teste é a guarda: se o `conftest` parar de fixar o idioma, ele grita aqui
    em vez de a suíte inteira falhar num ambiente e não no outro, apontando para o
    lugar errado.
    """
    from conftest import SUITE_LANGUAGE

    assert i18n.lang() == SUITE_LANGUAGE
    assert i18n.lang_source() == "TA_LANG", "o idioma está sendo herdado, não fixado"
