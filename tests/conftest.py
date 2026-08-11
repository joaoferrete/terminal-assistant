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

# O idioma da suíte é FIXADO, e não herdado da máquina.
#
# Sem isto, `i18n.lang()` cai no locale de quem roda: na máquina do autor
# (`pt_BR.UTF-8`) o parser ficava em português e os 17 testes de `@sexta`,
# `3 de fevereiro` e `hoje eu preciso` passavam; no runner do CI, que não define
# `LANG`, o padrão vira `en` e todos falhavam.
#
# Foi o CI que pegou, na primeira execução — que é exatamente para isso que ele
# serve. Um teste que depende do ambiente de quem o roda não está testando o
# código, está testando a máquina.
#
# `pt` porque é o que a maioria dos testes de parser exercita. Quem precisa do
# outro idioma o declara por teste, como `test_i18n` faz.
IDIOMA_DA_SUITE = "pt"


@pytest.fixture(autouse=True)
def _caches_limpos(monkeypatch):
    from ta import config, i18n

    monkeypatch.setenv("TA_LANG", IDIOMA_DA_SUITE)
    i18n.reset_cache()
    config._user_config.cache_clear()
    yield
    i18n.reset_cache()
    config._user_config.cache_clear()
