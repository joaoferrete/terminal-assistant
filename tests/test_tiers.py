"""Model Tiers (D53) and honest Tool errors (D52a)."""

from test_agent import answer, call, make, say  # noqa: F401 - the fixture

from ta import config as cfg_mod
from ta.llm import LLM
from ta.providers import DeepSeekProvider, GeminiProvider
from ta.tools import tool


def llm(tiers=None, routes=None):
    return LLM(providers={"deepseek": DeepSeekProvider("k"), "gemini": GeminiProvider("g")},
               default="deepseek", fallback="gemini", routes=routes, tiers=tiers)


def test_without_tiers_nothing_changes():
    assert [p.model for p in llm().chain("agent")] == ["deepseek-flash", llm().chain()[1].model]


def test_the_chat_goes_pro_and_the_reviews_stay_lite():
    m = llm(tiers={"pro": "deepseek:deepseek-v4-pro", "lite": "deepseek:deepseek-flash"})
    agent, review = m.chain("agent"), m.chain("review_capture")
    assert agent[0].model == "deepseek-v4-pro" and agent[0].name == "deepseek"
    assert review[0].model == "deepseek-flash"
    assert agent[1].name == review[1].name == "gemini", "one fallback for every Tier"
    assert m.providers["deepseek"].model == "deepseek-flash", "the base provider is untouched"


def test_a_task_route_still_wins_and_may_name_a_tier_or_a_model():
    m = llm(tiers={"pro": "deepseek:deepseek-v4-pro"},
            routes={"organize": "gemini", "classify": "pro", "digest_prose": "deepseek:x"})
    assert m.chain("organize")[0].name == "gemini"
    assert m.chain("classify")[0].model == "deepseek-v4-pro"
    assert m.chain("digest_prose")[0].model == "x"


def test_the_config_reads_tiers_and_drops_nonsense(tmp_path, monkeypatch):
    (tmp_path / "ta").mkdir()
    (tmp_path / "ta" / "config.toml").write_text(
        '[llm.tiers]\npro = "deepseek:deepseek-v4-pro"\nlite = "gpt"\n'
        '[llm.tasks]\norganize = "gemini"\nclassify = "pro"\nagent = "gpt:4"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    assert cfg_mod.llm_tiers() == {"pro": "deepseek:deepseek-v4-pro"}
    assert cfg_mod.llm_routing()[2] == {"organize": "gemini", "classify": "pro"}


# ── Honest errors ───────────────────────────────────────────────────────────
calls = []


@tool(description="flaky (test)", args={"x": "x"}, name="test_flaky")
async def _flaky(ctx, x):
    from ta.actuators.home import HomeError

    calls.append(x)
    raise HomeError("Home Assistant refused: light.sala does not support colour")


def test_the_model_reads_why_and_the_same_call_is_not_repeated(make):  # noqa: F811
    calls.clear()
    b = make(call("test_flaky", '{"x": "1"}'), call("test_flaky", '{"x": "1"}'),
             answer("Essa luz não aceita cor."))
    assert say(b, "luz azul") == "Essa luz não aceita cor."
    assert calls == ["1"], "the identical second call never ran"
    last = b.llm.prompts[-1][1]
    assert "does not support colour" in last and "already failed" in last


def test_a_bugs_message_is_not_shown_only_its_kind():
    from ta.agent import _reason

    assert "secret" not in _reason(ValueError("secret=abc"))
    assert "ValueError" in _reason(ValueError("x"))
