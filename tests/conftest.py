"""Isolation that holds for the whole suite.

The language and the user configuration are resolved once per process and then
sit in an `lru_cache` — the right thing in production, where none of it changes
while the daemon is up, and poison in a test process, where one test that pins
`TA_LANG=en` leaves the cache that way for everything that runs after it.

It really happened: `test_i18n` started running before `test_notes` and thirteen
Portuguese parser tests began failing with nothing related to them having
changed. The symptom points at the wrong place, which is what makes this kind of
coupling expensive.
"""
import pytest

# The suite's language is PINNED, not inherited from the machine.
#
# Without this, `i18n.lang()` falls back to the locale of whoever runs it: on the
# author's machine (`pt_BR.UTF-8`) the parser stayed Portuguese and the 17 tests
# for `@sexta`, `3 de fevereiro` and `hoje eu preciso` passed; on the CI runner,
# which sets no `LANG`, the default becomes `en` and every one of them failed.
#
# CI caught it on the very first run — which is exactly what CI is for. A test
# that depends on the environment of whoever runs it is not testing the code, it
# is testing the machine.
#
# `pt` because that is what most of the parser tests exercise. Anything that
# needs the other language declares it per test, the way `test_i18n` does.
SUITE_LANGUAGE = "pt"


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    from ta import config, i18n

    monkeypatch.setenv("TA_LANG", SUITE_LANGUAGE)
    i18n.reset_cache()
    config._user_config.cache_clear()
    yield
    i18n.reset_cache()
    config._user_config.cache_clear()
