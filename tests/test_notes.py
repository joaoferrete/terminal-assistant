from datetime import date, datetime

from ta.notes import parse

# Segunda-feira, 10 de agosto de 2026, 09:00.
NOW = datetime(2026, 8, 10, 9, 0)


def p(raw):
    return parse(raw, now=NOW)


def test_texto_sem_marca_continua_valido():
    """A sintaxe é opcional. Este é o caso mais importante do módulo."""
    n = p("ideia: usar cache no pooler")
    assert n.text == "ideia: usar cache no pooler"
    assert not n.is_task and not n.is_reminder
    assert n.tags == [] and n.priority is None


def test_extrai_tag_prioridade_e_prazo():
    n = p("ligar pro dentista #saude !alta @sexta")
    assert n.text == "ligar pro dentista"
    assert n.tags == ["saude"]
    assert n.priority == "alta"
    assert n.due == date(2026, 8, 14)  # sexta seguinte
    assert n.is_task


def test_reminder_nao_e_confundido_com_prioridade():
    """`!!22:00` tem que ser lido antes de `!`, senão sobra lixo no texto."""
    n = p("tomar remedio !!22:00")
    assert n.text == "tomar remedio"
    assert n.remind_at == datetime(2026, 8, 10, 22, 0)
    assert n.priority is None
    assert n.is_reminder and not n.is_task


def test_reminder_com_hora_passada_vai_para_amanha():
    n = p("dormir !!07:00")  # agora são 09:00
    assert n.remind_at == datetime(2026, 8, 11, 7, 0)


def test_dia_da_semana_atual_significa_a_proxima():
    """@segunda numa segunda é a que vem — para hoje existe @hoje."""
    assert p("x @segunda").due == date(2026, 8, 17)
    assert p("x @hoje").due == date(2026, 8, 10)
    assert p("x @amanha").due == date(2026, 8, 11)


def test_formatos_de_data():
    assert p("x @2026-12-25").due == date(2026, 12, 25)
    assert p("x @25/12").due == date(2026, 12, 25)
    assert p("x @25/12/2027").due == date(2027, 12, 25)


def test_data_sem_ano_que_ja_passou_vai_para_o_ano_seguinte():
    assert p("x @01/01").due == date(2027, 1, 1)


def test_token_que_nao_e_data_fica_no_texto():
    """@casa não é prazo, e não deve virar erro nem desaparecer."""
    n = p("comprar lampada @casa")
    assert n.due is None
    assert "@casa" in n.text


def test_hora_invalida_e_ignorada():
    n = p("x !!99:99")
    assert n.remind_at is None
    assert "!!99:99" in n.text


def test_multiplas_tags_sem_duplicata_e_ordenadas():
    n = p("x #b #a #a")
    assert n.tags == ["a", "b"]


def test_marca_no_meio_do_texto():
    n = p("ligar #saude pro dentista @sexta agora")
    assert n.text == "ligar pro dentista agora"
    assert n.tags == ["saude"]


def test_nao_confunde_marca_colada_em_palavra():
    """`a#b` e `email@dominio` não são marcas."""
    n = p("mandar email@dominio.com sobre C#coisa")
    assert n.tags == []
    assert n.due is None
    assert n.text == "mandar email@dominio.com sobre C#coisa"


# ── Data e hora em português corrente (sem marca) ───────────────────────────
def test_prazo_em_portugues_corrente():
    """`Revisar o PR do Ana hoje` tem que virar tarefa de hoje, sem `@`."""
    n = parse("Revisar o PR do Ana hoje", now=NOW)
    assert n.due == date(2026, 8, 10)
    assert n.is_task
    # O texto NÃO é mutilado: a palavra faz parte da frase.
    assert n.text == "Revisar o PR do Ana hoje"


def test_dia_e_mes_com_hora_viram_prazo_e_lembrete():
    n = parse("ir na nutricionista 17 de setembro as 8:30", now=NOW)
    assert n.due == date(2026, 9, 17)
    assert n.remind_at == datetime(2026, 9, 17, 8, 30)


def test_dia_da_semana_com_preposicao():
    """`na terça` resolve para a próxima terça; AGORA é uma segunda."""
    n = parse("reunião com o cliente na terça às 14h", now=NOW)
    assert n.due == date(2026, 8, 11)
    assert n.remind_at == datetime(2026, 8, 11, 14, 0)


def test_mes_que_ja_passou_vai_para_o_ano_que_vem():
    assert parse("aniversário 3 de fevereiro", now=NOW).due == date(2027, 2, 3)


def test_marca_explicita_vence_a_linguagem_natural():
    """`@sexta` ganha de `hoje` no mesmo texto: o explícito manda."""
    n = parse("revisar isso hoje @sexta", now=NOW)
    assert n.due == date(2026, 8, 14)


def test_hora_solta_sem_data_nao_e_lembrete():
    """`rodar 8h de bateria` não é compromisso — exige preposição."""
    n = parse("rodar 8h de bateria", now=NOW)
    assert n.remind_at is None and n.due is None


def test_numero_solto_nao_vira_nada():
    for texto in ("ler o PR 42 do time", "comprar 2 de leite"):
        n = parse(texto, now=NOW)
        assert (n.due, n.remind_at) == (None, None), texto


def test_frase_retrospectiva_nao_ganha_prazo():
    """`hoje aprendi X` é relato, não prazo. Era o falso positivo mais provável."""
    for texto in (
        "hoje aprendi sobre WAL no sqlite",
        "hoje eu aprendi a usar pointer events",
        "hoje foi difícil",
        "hoje descobri o bug do domínio",
        "amanhã vi o resultado",
    ):
        assert parse(texto, now=NOW).due is None, texto


def test_retrospectivo_nao_engole_prazo_legitimo():
    """A guarda não pode ser tão larga que mate `hoje eu preciso revisar`."""
    for texto in ("hoje eu preciso revisar o PR", "terminar isso hoje", "dentista amanhã"):
        assert parse(texto, now=NOW).due is not None, texto


# ── Marca de lembrete: @@ no lugar de !! ────────────────────────────────────
def test_arroba_dupla_e_a_marca_de_lembrete():
    """`!!` dispara expansão de histórico no zsh e o comando morre antes de
    chegar aqui: `zsh: no such word in event`. Medido, não suposto."""
    n = parse("tomar remedio @@22:00", now=NOW)
    assert n.remind_at == datetime(2026, 8, 10, 22, 0)
    assert n.is_reminder
    assert n.text == "tomar remedio"


def test_bang_duplo_continua_aceito():
    """No mural não há shell, e quebrar nota já escrita não compra nada."""
    assert parse("tomar remedio !!22:00", now=NOW).remind_at == datetime(2026, 8, 10, 22, 0)


def test_arroba_dupla_convive_com_arroba_de_data():
    n = parse("reuniao @sexta @@14:30", now=NOW)
    assert n.due == date(2026, 8, 14)
    assert n.remind_at == datetime(2026, 8, 10, 14, 30)
    assert n.text == "reuniao"


def test_hora_invalida_fica_no_texto():
    n = parse("@@25:99 hora invalida", now=NOW)
    assert n.remind_at is None
    assert "@@25:99" in n.text


def test_email_nao_e_confundido_com_marca():
    n = parse("mandar pro email@dominio.com", now=NOW)
    assert (n.due, n.remind_at) == (None, None)
    assert n.text == "mandar pro email@dominio.com"
