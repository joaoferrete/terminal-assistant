"""A regra que motivou o projeto.

"Entrei numa reunião do Meet e são mais de 16h, então acende a luz no máximo — e
seria PERFEITO se eu pudesse combinar isso com abrir o ringlight."

É o caso que nenhum motor pronto resolve: o gatilho nasce no PC (microfone), a
condição é de hora, e as ações caem metade na casa e metade no PC. Por isso o app
é o cérebro e o Home Assistant é atuador burro (ADR 0002).

Este arquivo é versionado de propósito. Editar e salvar; `ta rules check` valida
sem subir o daemon, e o daemon recarrega no restart.
"""

from ta.engine import after, mic_active, mic_inactive, rule

# Apelido do inventário real da casa, conferido via API do HA.
LUZ = "light.lampada_do_quarto"
PROFILE_RINGLIGHT = "Meet"


@rule(on=mic_active(), when=after("16:00"), name="reuniao_tarde")
async def reuniao_tarde(ctx):
    """Depois das 16h, chamada com microfone ativo acende luz e ringlight."""
    # A agenda entra como contexto, não como gatilho (ADR 0008): ela diz QUAL
    # reunião é, e permite decidir diferente por tipo de compromisso.
    evento = await ctx.calendar.agora() if ctx.calendar else None
    titulo = (evento or {}).get("summary", "")

    # 1:1 não precisa de produção. Exemplo de condição que só existe porque o
    # app tem acesso à agenda além do sinal do microfone.
    if titulo and "1:1" in titulo:
        return

    await ctx.home.light(LUZ, 100)
    await ctx.lighter.apply_profile(PROFILE_RINGLIGHT)

    if titulo:
        await ctx.notify.send("Reunião", f"{titulo} — luz e ringlight ligados", urgency="low")


@rule(on=mic_inactive(), name="fim_da_reuniao")
async def fim_da_reuniao(ctx):
    """Saiu da chamada: apaga o ringlight e devolve a luz a um nível de estar.

    Apagar por completo seria hostil — a pessoa continua no quarto.
    """
    await ctx.lighter.enable(False)
    await ctx.home.light(LUZ, 40)
