from datetime import datetime, timedelta

from ta.scheduler import Scheduler, atraso_de, texto_de_atraso


def make():
    horas, reminders = [], []

    async def on_time(minuto, agora):
        horas.append(minuto)

    async def on_reminder(agora):
        reminders.append(agora)

    return Scheduler(on_time, on_reminder), horas, reminders


async def test_primeiro_tick_nao_dispara_gatilho_de_hora():
    """Subir o daemon às 16:00 não deve executar a regra das 16:00."""
    s, horas, _ = make()
    await s.tick(datetime(2026, 8, 10, 16, 0))
    assert horas == []


async def test_dispara_uma_vez_por_minuto_nao_por_tick():
    s, horas, _ = make()
    await s.tick(datetime(2026, 8, 10, 15, 59, 0))   # primeiro tick, só registra
    await s.tick(datetime(2026, 8, 10, 16, 0, 5))
    await s.tick(datetime(2026, 8, 10, 16, 0, 25))   # mesmo minuto
    await s.tick(datetime(2026, 8, 10, 16, 0, 45))   # mesmo minuto
    await s.tick(datetime(2026, 8, 10, 16, 1, 5))
    assert horas == ["16:00", "16:01"]


async def test_reminders_conferidos_a_cada_tick():
    s, _, reminders = make()
    for seg in (0, 20, 40):
        await s.tick(datetime(2026, 8, 10, 16, 0, seg))
    assert len(reminders) == 3


# ── Atraso ──────────────────────────────────────────────────────────────────
def test_atraso_pequeno_nao_ganha_rotulo():
    assert texto_de_atraso(timedelta(seconds=30)) == ""
    assert texto_de_atraso(timedelta(minutes=2)) == ""


def test_rotulo_de_atraso_em_minutos_e_horas():
    assert texto_de_atraso(timedelta(minutes=20)) == " (atrasado 20 min)"
    assert texto_de_atraso(timedelta(hours=3)) == " (atrasado 3 h)"


def test_atraso_grande_nao_finge_que_e_agora():
    """Subir o daemon depois de um fim de semana não deve avisar como se fosse agora."""
    assert texto_de_atraso(timedelta(days=2)) == " (muito atrasado)"


def test_atraso_de_calcula_a_partir_do_iso():
    agora = datetime(2026, 8, 10, 16, 30)
    assert atraso_de("2026-08-10T16:00:00", agora) == timedelta(minutes=30)
