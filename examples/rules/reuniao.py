"""A regra que motivou o projeto.

"Entrei numa reunião do Meet e são mais de 16h, então acende a luz no máximo — e
seria PERFEITO se eu pudesse combinar isso com abrir o ringlight."

É o caso que nenhum motor pronto resolve: o gatilho nasce no PC (microfone), a
condição é de hora, e as ações caem metade na casa e metade no PC. Por isso o app
é o cérebro e o Home Assistant é atuador burro (ADR 0002).

O pedido original amarrava a hora à **entrada** na chamada. Hoje a hora não decide
*se* a regra roda — ela decide **o que a regra faz**, porque o que `16:00` quer
dizer de verdade é "já escureceu":

- A **luz** acende no máximo em qualquer horário: numa call ela ajuda sempre.
- O **ringlight** só entra depois que escurece. De dia a luz natural já dá conta,
  e a borda acesa é produção que ninguém pediu.
- No **fim**, a luz volta ao nível de estar só se ainda for cedo. Depois disso ela
  fica onde está, porque devolvê-la ao nível de estar seria devolver o quarto ao
  escuro.

Este arquivo é versionado de propósito. Editar e salvar; `ta rules check` valida
sem subir o daemon, e o daemon recarrega no restart.
"""

from ta.engine import after, mic_active, mic_inactive, rule

# Apelido do inventário real da casa, conferido via API do HA.
LUZ = "light.lampada_do_quarto"
PROFILE_RINGLIGHT = "Meet"

HORA_DE_ESCURECER = "16:00"
NIVEL_DE_ESTAR = 40


def _ja_escureceu(ctx) -> bool:
    """Se `HORA_DE_ESCURECER` já passou.

    Um lugar só decide o que "16:00" quer dizer, e os dois usos leem daqui: ligar
    o ringlight na entrada, e segurar a luz na saída. É a mesma primitiva que uma
    `when=` usaria, então os três não podem divergir.

    A hora é lida no instante do disparo, não no início da chamada — uma reunião
    que atravessa as 16h conta como tarde na saída. Quem decide é a janela, não a
    agenda.
    """
    return after(HORA_DE_ESCURECER)(ctx)


async def _titulo_agora(ctx) -> str:
    """O título do compromisso em curso, ou vazio.

    A agenda entra como contexto, não como gatilho (ADR 0008): ela diz QUAL
    reunião é, e permite decidir diferente por tipo de compromisso.
    """
    evento = await ctx.calendar.agora() if ctx.calendar else None
    return (evento or {}).get("summary", "")


def _sem_producao(titulo: str) -> bool:
    """1:1 não precisa de produção.

    Exemplo de condição que só existe porque o app tem acesso à agenda além do
    sinal do microfone.
    """
    return bool(titulo) and "1:1" in titulo


@rule(on=mic_active(), name="reuniao")
async def reuniao(ctx):
    """Chamada com microfone ativo: luz sempre, ringlight só depois de escurecer."""
    titulo = await _titulo_agora(ctx)
    if _sem_producao(titulo):
        return

    await ctx.home.light(LUZ, 100)

    escuro = _ja_escureceu(ctx)
    if escuro:
        await ctx.lighter.apply_profile(PROFILE_RINGLIGHT)

    if titulo:
        # A notificação diz o que de fato aconteceu. Anunciar ringlight de manhã
        # seria mentira barata, e é assim que se deixa de confiar no aviso.
        o_que = "luz e ringlight ligados" if escuro else "luz ligada"
        await ctx.notify.send("Reunião", f"{titulo} — {o_que}", urgency="low")


@rule(on=mic_inactive(), name="fim_da_reuniao")
async def fim_da_reuniao(ctx):
    """Saiu da chamada: o ringlight sai sempre, a luz só cai se ainda for cedo.

    Apagar por completo seria hostil — a pessoa continua no quarto.

    O `enable(False)` é incondicional de propósito, mesmo que de manhã o ringlight
    nunca tenha sido aceso: desligar o que já está desligado não custa nada, e a
    alternativa — repetir aqui a condição da entrada — deixaria a borda acesa numa
    reunião que começou às 15h50 e terminou às 16h10.

    O 1:1 é conferido aqui também, e não só na entrada: se nada foi aceso, nada
    deve ser desfeito. A checagem depende de o compromisso ainda estar em curso
    (`calendar.now()` só devolve evento entre `start` e `end`), então quem sai
    **depois** da hora marcada cai no caminho comum e tem a luz ajustada. Errar
    para o lado de ajustar é o lado barato.
    """
    if _sem_producao(await _titulo_agora(ctx)):
        return

    await ctx.lighter.enable(False)

    if not _ja_escureceu(ctx):
        await ctx.home.light(LUZ, NIVEL_DE_ESTAR)
