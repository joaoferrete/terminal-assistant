"""The provider layer: routing, validation on our side, and falling back (ADR 0018).

A malformed answer is an expected path here, not an exception — DeepSeek only
promises *some* JSON, and its own docs say the content can come back empty. So
every way it can go wrong has a test, and none of them touches the network:
DeepSeek answers through `httpx.MockTransport`, Gemini is a fake.
"""
import asyncio
import json

import httpx
import pytest

from ta import config as cfg_mod
from ta.llm import LLM, TASKS, Prose, for_task
from ta.providers import DeepSeekProvider, LLMUnavailable, Usage


def deepseek(*answers, calls=None):
    """A DeepSeek that returns `answers` in order. An int is an HTTP error status."""
    pending = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        answer = pending.pop(0)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "boom"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return DeepSeekProvider("a-fake-key", transport=httpx.MockTransport(handler))


class FakeGemini:
    name = "gemini"
    model = "a-fake-gemini"

    def __init__(self, text="from gemini", configured=True, fails=False):
        self.text, self._configured, self.fails = text, configured, fails
        self.calls = 0

    @property
    def configured(self):
        return self._configured

    async def structured(self, prompt, schema, system):
        self.calls += 1
        if self.fails:
            raise LLMUnavailable("gemini down too")
        return schema(text=self.text), Usage(1, 1)

    async def list_models(self):
        return [self.model]


def routed(ds, gemini, **kw):
    kw.setdefault("default", "deepseek")
    kw.setdefault("fallback", "gemini")
    return LLM(providers={"deepseek": ds, "gemini": gemini}, **kw)


def ask(llm, task="digest_prose"):
    async def go():
        with for_task(task):
            return await llm._structured("a prompt", Prose)

    return asyncio.run(go())


# ── DeepSeek on its own ─────────────────────────────────────────────────────
def test_a_valid_answer_is_parsed_and_its_usage_counted():
    value, usage = asyncio.run(deepseek('{"text": "ok"}').structured("p", Prose, "sys"))
    assert value == Prose(text="ok")
    assert usage == Usage(10, 5)


def test_the_request_asks_for_json_mode_and_carries_the_schema():
    calls = []
    asyncio.run(deepseek('{"text": "ok"}', calls=calls).structured("p", Prose, "sys"))
    body = calls[0]
    assert body["response_format"] == {"type": "json_object"}
    # JSON mode refuses to work unless the word appears in the prompt.
    assert "JSON" in body["messages"][0]["content"]
    assert '"text"' in body["messages"][0]["content"]


def test_a_malformed_answer_gets_one_retry_that_sees_what_was_wrong():
    calls = []
    ds = deepseek("not json at all", '{"text": "fixed"}', calls=calls)
    value, usage = asyncio.run(ds.structured("p", Prose, "sys"))

    assert value == Prose(text="fixed")
    assert len(calls) == 2
    # The retry carries its own bad answer back, so it can correct it.
    assert calls[1]["messages"][-2] == {"role": "assistant", "content": "not json at all"}
    assert usage == Usage(20, 10), "both attempts cost tokens, so both are counted"


@pytest.mark.parametrize("bad", ["", "{}", '{"text": 3.5, "extra": ['])
def test_two_invalid_answers_make_the_provider_unavailable(bad):
    """Empty content is the failure DeepSeek's docs warn about by name."""
    with pytest.raises(LLMUnavailable):
        asyncio.run(deepseek(bad, bad).structured("p", Prose, "sys"))


def test_an_http_error_is_unavailable_not_a_crash():
    with pytest.raises(LLMUnavailable):
        asyncio.run(deepseek(500).structured("p", Prose, "sys"))


# ── Routing and fallback ────────────────────────────────────────────────────
def test_deepseek_is_the_default_when_both_are_configured():
    gemini = FakeGemini()
    assert ask(routed(deepseek('{"text": "from deepseek"}'), gemini)).text == "from deepseek"
    assert gemini.calls == 0


def test_malformed_twice_falls_back_to_gemini():
    gemini = FakeGemini()
    assert ask(routed(deepseek("nope", "nope"), gemini)).text == "from gemini"
    assert gemini.calls == 1


def test_a_deepseek_outage_falls_back_to_gemini():
    assert ask(routed(deepseek(503), FakeGemini())).text == "from gemini"


def test_when_every_provider_fails_the_caller_gets_unavailable():
    """The caller's own degradation (capture without review, the board without
    organize) depends on seeing `LLMUnavailable`, never a stray exception."""
    with pytest.raises(LLMUnavailable):
        ask(routed(deepseek(500), FakeGemini(fails=True)))


def test_a_gemini_only_installation_keeps_working_unchanged():
    """Every installation before DeepSeek has only GEMINI_API_KEY. The DeepSeek
    default must be skipped, not tried and failed."""
    ds = DeepSeekProvider(None)
    llm = routed(ds, FakeGemini())
    assert llm.chain("organize") == [llm.providers["gemini"]]
    assert ask(llm).text == "from gemini"
    assert llm.model == "a-fake-gemini"


def test_no_provider_at_all_is_unavailable_with_the_fix_in_the_message():
    llm = routed(DeepSeekProvider(None), FakeGemini(configured=False))
    assert not llm.configured
    with pytest.raises(LLMUnavailable, match="DEEPSEEK_API_KEY"):
        ask(llm)


def test_a_task_route_overrides_the_default():
    gemini = FakeGemini()
    llm = routed(deepseek('{"text": "from deepseek"}'), gemini, routes={"organize": "gemini"})
    assert ask(llm, task="organize").text == "from gemini"
    assert ask(llm, task="digest_prose").text == "from deepseek"


def test_usage_is_reported_with_the_provider_and_the_task():
    seen = []
    llm = routed(deepseek('{"text": "ok"}'), FakeGemini())
    llm.on_usage = lambda provider, model, task, usage: seen.append(
        (provider, model, task, usage)
    )
    ask(llm, task="review_capture")
    assert seen == [("deepseek", "deepseek-flash", "review_capture", Usage(10, 5))]


def test_the_task_methods_route_by_their_own_name():
    """The decorator, not the caller, names the task — or the per-task route in
    config.toml would never be reached from the daemon."""
    seen = []
    llm = routed(deepseek('{"text": "a sentence"}'), FakeGemini())
    llm.on_usage = lambda provider, model, task, usage: seen.append(task)
    asyncio.run(llm.digest_prose([], []))
    assert seen == ["digest_prose"]


# ── config.toml ─────────────────────────────────────────────────────────────
@pytest.fixture
def user_config(tmp_path, monkeypatch):
    def write(text: str):
        (tmp_path / "ta").mkdir(exist_ok=True)
        (tmp_path / "ta" / "config.toml").write_text(text)
        cfg_mod._user_config.cache_clear()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    return write


def test_the_default_routing_is_deepseek_then_gemini(user_config):
    user_config("")
    assert cfg_mod.llm_routing() == ("deepseek", "gemini", {})


def test_routing_is_read_from_config_toml(user_config):
    user_config(
        '[llm]\ndefault = "gemini"\nfallback = "deepseek"\n'
        '[llm.tasks]\norganize = "deepseek"\n'
    )
    assert cfg_mod.llm_routing() == ("gemini", "deepseek", {"organize": "deepseek"})


def test_a_typo_in_a_provider_name_is_dropped_and_logged(user_config, caplog):
    user_config('[llm]\ndefault = "deepseak"\n[llm.tasks]\norganize = "gpt"\n')
    assert cfg_mod.llm_routing() == ("deepseek", "gemini", {})
    assert "deepseak" in caplog.text and "gpt" in caplog.text


def test_the_fallback_can_be_turned_off(user_config):
    user_config('[llm]\nfallback = ""\n')
    assert cfg_mod.llm_routing()[1] is None


def test_the_documented_tasks_are_the_routable_ones():
    """`configuration.md` lists these; a renamed task would ignore its route."""
    doc = (cfg_mod.Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
    missing = [t for t in TASKS if t not in doc]
    assert not missing, f"tasks not documented in configuration.md: {missing}"
