"""Isolamento que vale para a suíte inteira.

O idioma e a configuração do usuário são resolvidos uma vez por processo e
ficam em `lru_cache` — é o certo em produção, onde nada disso muda com o daemon
de pé, e é veneno num processo de teste, onde um teste que fixa `TA_LANG=en`
deixa o cache assim para todos os que vierem depois.

Aconteceu de verdade: `test_i18n` passou a rodar antes de `test_notes` e treze
testes de parser em português começaram a falhar sem que nada relacionado a eles
tivesse mudado. O sintoma aponta para o lugar errado, que é o que torna esse tipo
de acoplamento caro.
"""
import pytest


@pytest.fixture(autouse=True)
def _caches_limpos():
    from ta import config, i18n

    i18n.reset_cache()
    config._user_config.cache_clear()
    yield
    i18n.reset_cache()
    config._user_config.cache_clear()
