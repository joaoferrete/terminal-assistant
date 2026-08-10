# Automações

Este é o guia da parte que motivou o projeto: **combinar o estado do PC com o
estado da casa.** É o que nenhum motor pronto resolve, porque o Home Assistant
não sabe que você entrou numa call e o GNOME não sabe acender uma lâmpada.

## O modelo mental

Uma automação é uma **Rule**, e uma Rule tem três partes:

```
   gatilho          condição            ações
   ───────          ────────            ─────
   entrei numa  +   e passou das   →    acende a luz no máximo
   call             16h                 e liga o ringlight
```

O gatilho é o **quando**, a condição é o **só se**, e as ações são o **então**.
Só o gatilho é obrigatório.

As Rules são arquivos Python em `rules/`, versionados junto do código. Não é uma
linguagem de template: é Python de verdade, com `if`, com variáveis, com o que
você quiser. A razão está no [ADR 0002](adr/0002-motor-de-automacao-proprio-em-python.md)
— um YAML exigiria escrever um interpretador antes da primeira regra rodar.

## A primeira regra, em três minutos

Crie um arquivo qualquer em `rules/`. O nome não importa, só a extensão:

```python
# rules/boa_noite.py
from ta.engine import at_time, rule


@rule(on=at_time("23:00"))
async def boa_noite(ctx):
    """Às 23h, baixa a luz em vez de apagar. Apagar seria hostil."""
    await ctx.home.switch_on("light.lampada_do_quarto", 15)
    await ctx.notify.send("Boa noite", "luz baixada")
```

Valide sem subir nada:

```bash
ta rules check
```

```
  ok   reuniao_tarde  (mic=True)
  ok   fim_da_reuniao  (mic=False)
  ok   boa_noite  (time=23:00)

3 regra(s), 0 com erro.
```

E ponha no ar:

```bash
systemctl --user restart ta
ta rules              # confirma que o daemon carregou
```

⚠️ **O daemon não recarrega regras sozinho.** Editar o arquivo não basta; precisa
do restart. Isso é deliberado — recarregar código em processo residente é fonte de
bugs difíceis, e um restart custa menos de um segundo.

## Gatilhos disponíveis

| Gatilho | Dispara quando | De onde vem o sinal |
|---|---|---|
| `mic_active()` | o microfone passa a ser usado | PipeWire, `pw-dump`, a cada segundo |
| `mic_inactive()` | o microfone deixa de ser usado | idem |
| `at_time("HH:MM")` | o relógio bate naquele minuto | scheduler interno |
| `reminder_due()` | um Reminder vence | as suas próprias notas |
| `entity_state("entity_id")` | aquela entity muda de estado | polling do HA, a cada 5s |

Detalhes que importam na prática:

- **`mic_*` tem debounce de 2 segundos.** Um bipe do sistema não dispara reunião.
  E `gnome-shell` e `gsd-media-keys` são ignorados de propósito: eles abrem o
  microfone sem que ninguém esteja em call.
- **`at_time` é de minuto**, não de segundo, e **nunca dispara no primeiro tick**
  depois de o daemon subir. Se subisse às 23:00:30 sem essa guarda, a regra das
  23:00 dispararia atrasada e errada.
- **`entity_state` só vê a segunda leitura em diante.** A primeira estabelece a
  linha de base, então subir o daemon não gera uma enxurrada de "mudou de estado".

## Condições

Condições entram no `when=` e recebem o mesmo `ctx`:

| Condição | Verdadeira quando |
|---|---|
| `after("16:00")` | agora é 16:00 ou depois |
| `before("09:00")` | agora é antes de 09:00 |
| `all_of(a, b, ...)` | todas verdadeiras |
| `any_of(a, b, ...)` | alguma verdadeira |

E `when=` aceita **qualquer** função que receba `ctx` e devolva bool, então você
não está preso a essas quatro:

```python
from ta.engine import all_of, at_time, before, rule


def dia_de_semana(ctx):
    return ctx.now.weekday() < 5


@rule(on=at_time("08:30"), when=all_of(dia_de_semana, before("12:00")))
async def bom_dia_de_trabalho(ctx): ...
```

## O que a Rule recebe: `ctx`

| Campo | O que é |
|---|---|
| `ctx.now` | `datetime` do momento do disparo |
| `ctx.trigger` | o gatilho que disparou (`.kind` e `.value`) |
| `ctx.home` | a casa, via Home Assistant |
| `ctx.lighter` | o ringlight |
| `ctx.notify` | notificação de desktop |
| `ctx.calendar` | a agenda (as duas contas) |
| `ctx.note` | a nota, **só** quando o gatilho é `reminder_due()` |
| `ctx.extra` | dados do gatilho (veja abaixo) |

O `ctx.extra` muda conforme o gatilho:

| Gatilho | Conteúdo do `extra` |
|---|---|
| `mic_*` | `{"apps": ["chrome", ...]}` — quem está usando o microfone |
| `entity_state` | `{"from": "off", "to": "on"}` |

### `ctx.home` — a casa

```python
await ctx.home.switch_on("light.lampada_do_quarto", 100)  # brilho só em light.
await ctx.home.switch_on("switch.ventilador_socket_1")    # sem brilho
await ctx.home.turn_off("light.lampada_do_quarto")
estado = await ctx.home.state("light.lampada_do_quarto")  # dict do HA
tudo = await ctx.home.entities("light.", "switch.")       # o inventário
casa = await ctx.home.sensors()                           # clima, roteador, tomada
```

⚠️ `switch_on` deriva o domínio do `entity_id`. Não chame `light/turn_on` num
`switch` — o HA responde **200 e não faz nada**, que é o pior tipo de falha. Foi
um bug real deste projeto.

### `ctx.lighter` — o ringlight

O ringlight é o [Lighter](https://github.com/joaoferrete/Lighter), extensão do GNOME mantida no mesmo
GitHub. O daemon a comanda por `gsettings`.

```python
await ctx.lighter.apply_profile("Meet")   # por nome, nunca por uid
await ctx.lighter.enable(False)
await ctx.lighter.toggle()
perfis = await ctx.lighter.profiles()
```

A **calibração mora na extensão**, não aqui. A Rule diz *qual* profile; a Lighter
sabe *como* ele é. Duplicar os 9 valores de borda numa Rule seria criar uma
segunda fonte de verdade.

Aplicar profile de fora não funcionava até um
[PR na extensão](https://github.com/joaoferrete/Lighter/pull/3): a chave `active-profile` existia, mas só o
processo de preferências a escutava, e só para atualizar a própria interface. Escrever
a chave de fora não mudava nada na tela.

### `ctx.calendar` — a agenda

```python
evento = await ctx.calendar.agora()   # o compromisso em curso, ou None
eventos = await ctx.calendar.hoje()   # todos os de hoje
```

Cada evento é um dict com `summary`, `start`, `end`, `all_day`, `calendar`.

A agenda é **contexto, não gatilho** ([ADR 0008](adr/0008-microfone-como-gatilho-de-reuniao.md)):
o microfone diz que você está em reunião, a agenda diz *qual*. Um evento no
calendário não prova que você entrou nele.

### `ctx.notify` — a tela

```python
await ctx.notify.send("Título", "corpo", urgency="critical")
```

`urgency` aceita `low`, `normal`, `critical`. **Nunca levanta exceção**: um aviso
que falha não deve derrubar a Rule que o pediu.

## Receitas

Todas usam o inventário real da casa. Copie, ajuste, salve em `rules/`.

### 1. Reunião depois das 16h (a que já existe)

O caso original, em `rules/reuniao.py`. Note as duas coisas que só são possíveis
porque o app é o cérebro: a condição de hora, e consultar a agenda para tratar
1:1 diferente.

```python
from ta.engine import after, mic_active, mic_inactive, rule

LUZ = "light.lampada_do_quarto"


@rule(on=mic_active(), when=after("16:00"))
async def reuniao_tarde(ctx):
    evento = await ctx.calendar.agora() if ctx.calendar else None
    titulo = (evento or {}).get("summary", "")

    if titulo and "1:1" in titulo:      # 1:1 não precisa de produção
        return

    await ctx.home.switch_on(LUZ, 100)
    await ctx.lighter.apply_profile("Meet")


@rule(on=mic_inactive())
async def fim_da_reuniao(ctx):
    await ctx.lighter.enable(False)
    await ctx.home.switch_on(LUZ, 40)
```

### 2. Lembrete que acende a luz

Fecha o ciclo entre os dois módulos: uma nota sua vira uma ação na casa.

```python
import asyncio

from ta.engine import reminder_due, rule


@rule(on=reminder_due())
async def lembrete_piscando(ctx):
    """Lembrete marcado com #urgente pisca a luz, além do aviso na tela."""
    if "urgente" not in (ctx.note or {}).get("tags", []):
        return

    for _ in range(3):
        await ctx.home.switch_on("light.lampada_do_quarto", 100)
        await asyncio.sleep(0.4)
        await ctx.home.switch_on("light.lampada_do_quarto", 20)
        await asyncio.sleep(0.4)
```

Não precisa notificar na tela: o daemon já fez isso **antes** de chamar a sua
Rule. O aviso de desktop é o caminho obrigatório e nunca depende de regra.

### 3. Digest falado de manhã

```python
from ta.engine import at_time, rule


@rule(on=at_time("08:30"))
async def bom_dia(ctx):
    eventos = await ctx.calendar.hoje()
    if not eventos:
        return

    primeiro = eventos[0]
    hora = primeiro["start"][11:16]
    await ctx.notify.send(
        "Bom dia",
        f"{len(eventos)} compromissos. Primeiro: {primeiro['summary']} às {hora}",
    )
```

### 4. Reagir a algo que mudou na casa

```python
from ta.engine import entity_state, rule


@rule(on=entity_state("switch.ventilador_socket_1"))
async def ventilador_mudou(ctx):
    if ctx.extra.get("to") != "on":
        return
    casa = await ctx.home.sensors()
    temp = casa["weather"]["temperature"]
    if temp is not None and temp < 20:
        await ctx.notify.send("Ventilador ligou", f"mas está {temp}°C lá fora")
```

Isto dispara em **qualquer** mudança daquela entity, inclusive as feitas pelo
aplicativo da Tuya ou pela Alexa — o polling não sabe quem mandou.

### 5. Chegou a hora do compromisso

```python
from ta.engine import at_time, rule


@rule(on=at_time("08:55"))
async def aviso_de_daily(ctx):
    for e in await ctx.calendar.hoje():
        if "Daily" in e["summary"]:
            await ctx.notify.send("Daily em 5 min", e["summary"], urgency="critical")
            await ctx.home.switch_on("light.lampada_do_quarto", 100)
            return
```

### 6. Uma condição própria, com o microfone e a agenda juntos

```python
from ta.engine import all_of, mic_active, rule


def em_reuniao_de_trabalho(ctx):
    """Só vale se o compromisso vier da agenda de trabalho."""
    return ctx.extra.get("apps") and any(
        "chrome" in app or "meet" in app for app in ctx.extra["apps"]
    )


@rule(on=mic_active(), when=all_of(em_reuniao_de_trabalho))
async def foco(ctx):
    await ctx.home.switch_on("switch.ventilador_socket_1")   # menos calor, menos ruído de fundo
```

## Quando não funciona

**A regra não aparece em `ta rules`.** Rode `ta rules check` — ele valida sem o
daemon e mostra o traceback. Arquivos que começam com `_` são ignorados de
propósito.

**A regra aparece mas não faz nada.** Exceção dentro de uma Rule é registrada e
**engolida**, para que uma regra ruim não leve as outras junto. O log tem a
resposta:

```bash
journalctl --user -u ta -f | grep -i regra
```

**Um arquivo com erro de sintaxe não derruba o daemon.** Ele tira daquele arquivo
do ar e os outros seguem carregando. É a garantia do ADR 0002, e é por isso que
`load_rules` captura `BaseException` — até um `exit()` perdido num arquivo de
regra é contido.

**Mudei a regra e nada mudou.** Falta o `systemctl --user restart ta`.

**A regra de ringlight não aplica o profile.** Duas causas possíveis, nesta ordem:

1. No Wayland o GNOME Shell não recarrega extensão, então **alterar o código** da
   [Lighter](https://github.com/joaoferrete/Lighter) só vale depois de logout/login. Mudança de
   *configuração* vale na hora.
2. O *auto-switch* da própria extensão está ligado e sobrescreve o que a Rule fez no
   próximo `notify::focus-window`. O daemon desliga essa chave ao subir, e a devolve
   ao sair — se ele morreu de morte matada, a chave fica desligada.

## Os limites de hoje

Vale saber o que **não** existe, para não procurar:

- **Sem gatilho de janela ou de app em foco.** No Wayland nenhum processo externo
  lê título de janela. A extensão Lighter *pode* (roda dentro do Shell), e é o
  caminho se um dia isso virar necessário — mas hoje o sinal de reunião é o
  microfone.
- **Sem gatilho de câmera.** `/dev/video*` daria, e não foi preciso: em call de
  trabalho o microfone abre e a câmera muitas vezes não.
- **Sem `at_time` recorrente por dia da semana** na assinatura do gatilho. Faça
  no `when=`, como na receita 3.
- **Sem estado empurrado do HA.** É polling de 5s ([ADR 0001](adr/0001-home-assistant-como-camada-de-device.md)).
  Se algum dia precisar de latência sub-segundo, WebSocket entra em
  `actuators/home.py` sem mexer em quem chama.
