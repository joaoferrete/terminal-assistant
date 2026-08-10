# Terminal Assistant

Assistente pessoal de linha de comando para uma máquina só. Dois módulos, uma
aplicação:

- **Casa** — controlar as luzes e a tomada do terminal ou por atalho de teclado, e
  automações que misturam estado do PC com estado da casa.
- **Notas** — um lugar para despejar lembretes, tarefas e ideias, com um mural
  visual, priorização assistida por LLM e integração com a agenda.

Não são dois programas num repositório. Os dois lados são consumidores do mesmo
**motor de gatilhos**: um lembrete é "gatilho de tempo → notificar"; a luz da
reunião é "gatilho de sinal do PC → agir na casa e no PC".

## Como funciona

```
 sinais                      daemon                        atuadores
 ──────                      ──────                        ─────────
 microfone (PipeWire) ──┐
 agenda (EDS/DBus) ─────┼──▶  motor de gatilhos  ──┬──▶ Home Assistant ─▶ Tuya
 hora / scheduler ──────┘        (rules/*.py)      ├──▶ Lighter (borda luminosa)
                                     │            ├──▶ notificação de desktop
 CLI ──HTTP──▶ :7777 ────────────────┤            └──▶ agenda (cria evento)
 navegador ──────────────▶ mural     │
                                  SQLite
```

O **Home Assistant** é a única camada que conhece protocolo de aparelho. O app
fala com ele por REST e nunca sabe o que é uma lâmpada Tuya — o que torna a
migração futura para Zigbee um pareamento, não uma reescrita.

Mudança de estado vem por **polling de 5s**, não WebSocket. O plano previa WS; a
troca foi deliberada e está justificada no docstring de `actuators/home.py`. Se um
dia um gatilho precisar de latência sub-segundo, WS entra ali sem mexer em quem
chama.

O **daemon** é um processo asyncio: servidor HTTP (atende o CLI e serve o mural),
scheduler, watcher de microfone e cliente do Home Assistant no mesmo loop. Ele
existe porque gatilho de tempo e de estado do PC não sobrevivem num CLI que roda
e morre.

## Requisitos

Este projeto é deliberadamente acoplado a este desktop: Ubuntu 24.04, GNOME,
Wayland, PipeWire.

**O Python é o do sistema, não o do brew.** O `gi` (PyGObject) usado para falar
com a agenda existe apenas em `/usr/bin/python3`. Ver
[ADR 0005](docs/adr/0005-python-do-sistema-por-causa-do-pygobject.md) — um
`python3 -m venv` feito por reflexo produz um ambiente onde `import gi` falha sem
explicação óbvia.

## Instalação

```zsh
# 1. Typelibs da agenda (não vêm por padrão)
sudo apt install gir1.2-ecal-2.0 gir1.2-edataserver-1.2

# 2. Venv no interpretador do SISTEMA, enxergando os pacotes dele
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .

# 3. Conferir que os bindings chegaram
.venv/bin/python -c "import gi; gi.require_version('ECal','2.0'); print('ok')"

# 4. Serviço
systemctl --user enable --now ta
```

### Contas e serviços

1. **Agenda** — Configurações → Contas Online → adicionar as duas contas Google
   (pessoal e trabalho). O GNOME cuida do OAuth e da renovação de token; o app
   apenas lê o que o Evolution Data Server já sincronizou.
2. **Agenda dedicada** — criar no Google Calendar uma agenda chamada
   `Terminal Assistant`. É onde eventos detectados a partir de notas são gravados,
   como camada separada e descartável.
3. **Home Assistant** — subir em Docker, adotar as lâmpadas e a tomada, e gerar um
   token de longa duração.
4. **Tuya local** *(opcional, recomendado)* — extrair `device_id` e `local_key` de
   cada aparelho no Tuya IoT Platform. Sem isso os comandos passam pela nuvem
   (300–800 ms e dependência de internet); com isso ficam locais e instantâneos.
   **É o passo mais chato do setup.**
5. **`GEMINI_API_KEY`** no ambiente do systemd unit, nunca no repositório.

## Inventário da casa

O app conhece **apenas `entity_id`** — nunca marca, protocolo, `device_id` da
Tuya ou IP. É o que torna a migração para Zigbee um pareamento no Home Assistant
em vez de uma alteração de código ([ADR 0001](docs/adr/0001-home-assistant-como-camada-de-device.md)).

Conferido contra o HA em 2026-08-10 via `GET /api/states`.

| `entity_id` | Device | O que é |
|---|---|---|
| `light.lampada_do_quarto` | Lâmpada Positivo | A única lâmpada. **Não há uma segunda** — o inventário inicial de "duas Positivo" não se confirmou |
| `switch.ventilador_socket_1` | Tomada Ekasa | A tomada em si, com um ventilador ligado nela. Liga/desliga apenas — o ventilador é comum, não tem controle de velocidade |
| `switch.ventilador_bloqueio_para_criancas` | Tomada Ekasa | O travamento infantil da **mesma** tomada. É *configuração*, não aparelho: grupos não o alcançam (veja abaixo) |
| — | Echo (1, no quarto) | **Sem `media_player` no HA.** HACS e `alexa_media_player` v5.15.7 estão instalados e carregados, mas o login da Amazon não completou, e sem config entry a integração não cria entity. `ta media` responde dizendo isso |

A tomada Ekasa é o exemplo canônico do glossário: **um Device, duas Entities.**
Um comando sempre endereça a Entity. Pedir para "desligar a tomada" sem dizer qual
Entity é ambíguo, e é por isso que o [`CONTEXT.md`](CONTEXT.md) separa os dois
termos.

### Grupos e ambientes

`ta on <termo>` resolve o termo em quatro etapas, e para na primeira que casa:

| Etapa | Exemplo | Resultado |
|---|---|---|
| 1. `entity_id` literal | `ta on light.lampada_do_quarto` | exatamente essa Entity |
| 2. Grupo | `ta on luz` / `luzes` / `tomada` / `tudo` | todas as Entities do domínio |
| 3. Apelido | `ta on ventilador` | o `entity_id` mapeado em `config.py` |
| 4. Ambiente, por trecho do nome | `ta on quarto` | tudo cujo `entity_id` ou nome amigável contenha o termo, ignorando acento e caixa |

Na etapa 4 a **luz tem precedência**: "ligar o quarto" quer dizer a lâmpada, não
uma tomada que por acaso esteja no quarto. Só se nenhuma luz casar é que os
`switch` entram.

⚠️ **Grupos e ambientes ignoram Entities de configuração.** `ta on tudo` ligava o
travamento infantil da tomada, que não é um aparelho que alguém queira "ligar". A
API REST do HA **não** expõe `entity_category`, então o sinal usado é a presença
de `device_class`: a tomada real tem `device_class: outlet`, e o travamento
infantil não tem `device_class` nenhum. Nomear a Entity explicitamente continua
funcionando — grupo é conservador, explícito é exato.

### Sensores

As 17 entidades `sensor` não são comandáveis, mas `ta router` e `ta temp` expõem
as que interessam. O resumo é **curado**, porque seis são do módulo Sun e quatro
do backup do HA — não é o que alguém quer ver ao perguntar "como está a casa".

| Comando | Mostra | Vem de |
|---|---|---|
| `ta router` | IP externo, download, upload | `sensor.s7_*` |
| `ta temp` | temperatura, condição, umidade, vento | `weather.forecast_casa` |
| `ta temp` | se o ventilador está **puxando energia** | `sensor.ventilador_energia` |

A temperatura também aparece no topo de `ta today`, e degrada em silêncio: se o HA
estiver fora do ar, o Digest continua funcionando sem a linha do clima.

A tomada medir corrente permite algo que o interruptor não diz: se o ventilador
está de fato consumindo, e não apenas "ligado".

⚠️ **Ler o estado logo depois de comandar devolve o valor antigo.** O comando vai
para a nuvem da Tuya e volta em 300–800ms, então `ta on` e `ta off` esperam o
estado virar antes de reportar, e dizem "não confirmado" se não virar. Sem isso,
ligar a luz reportava `off` e parecia não ter funcionado.

## Inventário das agendas

Conferido via Evolution Data Server em 2026-08-10. Duas contas Google conectadas
pelo GNOME Online Accounts ([ADR 0004](docs/adr/0004-agenda-via-gnome-online-accounts.md)).

| Agenda | Conta | Papel |
|---|---|---|
| a agenda do e-mail pessoal | pessoal | leitura |
| `Família` | pessoal | leitura |
| `Terminal Assistant` | pessoal | **escrita** |
| a agenda do e-mail de trabalho | trabalho | leitura |
| `Feriados no Brasil` | trabalho | leitura |
| `Terminal Assistant` | trabalho | **escrita** |
| `birthdays`, `system-calendar` | — | locais do EDS |

Eventos detectados a partir de notas vão para a `Terminal Assistant` da conta que
corresponde ao contexto, escolhida pelo LLM. Errar esse roteamento põe o evento na
camada descartável errada, nunca numa agenda real — ver a emenda no
[ADR 0007](docs/adr/0007-propor-e-confirmar-antes-de-escrever-na-agenda.md).

⚠️ **Duas armadilhas verificadas na prática:**

1. `system-calendar` se chama "Pessoal" mas é **local do EDS** — gravar ali não
   sincroniza com o Google e não aparece no celular. Não é a conta pessoal.
2. **Nunca fixar os UIDs das agendas no código.** Eles são gerados pelo EDS e
   mudam se a agenda for recriada. Resolver em tempo de execução por nome de
   exibição + conta pai.

Os Echo entram por aqui como qualquer outra Entity — o app não tem código de
Alexa ([ADR 0009](docs/adr/0009-alexa-entra-como-media-player-do-home-assistant.md)).
Anúncio de voz é sempre **adicional** à notificação de desktop, nunca substituto:
a integração da Alexa é a peça menos confiável do projeto, e o caminho frágil não
pode ser o único caminho.

### Como achar um `entity_id`

`entity_id` tem sempre a forma `dominio.nome` — com ponto. Se o valor não tem
ponto, não é `entity_id`.

No Home Assistant: **Ferramentas de Desenvolvedor → Estados**, e filtrar por
`switch.` (tomada) ou `light.` (lâmpada). Alternativa: **Configurações →
Dispositivos e Serviços → Tuya**, clicar no aparelho, e abrir a entidade dele.

### Segredos

O token de longa duração do Home Assistant fica em `.env`, na raiz do projeto,
que está no `.gitignore` e é lido pelo systemd unit via `EnvironmentFile`. O
mesmo vale para `GEMINI_API_KEY`.

Duas coisas **não** entram no repositório nem neste arquivo: o token do HA e o
`local_key` de qualquer aparelho Tuya. O `device_id` sozinho não controla nada,
mas o `local_key` sim.

## Uso

Tudo passa pelo `ta`. O CLI é um cliente HTTP fino: ele não faz nada sozinho,
conversa com o daemon em `:7777`.

```zsh
# Notas — offline, instantâneo, sem LLM
ta note "ligar pro dentista @sexta #saude !alta"
ta board                    # abre o mural no navegador
ta today                    # o dia: agenda + tarefas

# Casa
ta on luz                   # todas as luzes, no máximo
ta on quarto 40             # um ambiente, com brilho
ta off                      # apaga tudo que está aceso

# LLM, sob comando
ta organize                 # agrupa e ordena as notas, e grava
```

### Referência completa

**Notas**

| Comando | O que faz |
|---|---|
| `ta note "<texto>"` | Captura. A sintaxe leve está abaixo |
| `ta list` | As notas abertas |
| `ta list --all` | Inclui concluídas e canceladas |
| `ta done <id>` | Conclui. Repetir volta a abrir |
| `ta board` | Abre o mural no navegador |
| `ta export` | Despeja tudo em markdown no stdout |
| `ta rm <id>` | Apaga. **Reversível** — vai para a lixeira |
| `ta rm --list` | Mostra a lixeira |
| `ta restore <id>` | Tira da lixeira |
| `ta revise` | LLM reetiqueta todas as notas abertas (o mesmo que o botão do mural) |
| `ta capture-popup` | Popup de captura rápida (é o que o atalho global chama) |

**Sintaxe da captura.** Determinística, sem LLM, e o que não casa fica no texto:

| Marca | Exemplo | Efeito |
|---|---|---|
| `@data` | `@amanha`, `@sexta`, `@25/12`, `@2026-12-25` | vira **prazo** — a Note passa a ser Task |
| `@@HH:MM` | `@@22:00` | vira **lembrete** — dispara notificação na hora |
| `!prioridade` | `!alta`, `!media`, `!baixa` | prioridade |

⚠️ **A marca de lembrete é `@@`, não `!!`.** `!!` era a marca original e **não
funciona no zsh**: a expansão de histórico atua na leitura da linha e o comando
morre antes de chegar ao `ta`, com `zsh: no such word in event`. `@@` foi medido
no shell real. O paralelo é proposital — `@` é o dia, `@@` é o dia com hora. `!!`
continua aceito, porque no mural não existe shell e porque quebrar nota já escrita
não compra nada.
| `#tag` | `#saude` | etiqueta, e dá para ter várias |

O `@data` aceita quatro formas, todas resolvidas na captura, sem LLM:

| Forma | Aceita | Regra |
|---|---|---|
| Relativa | `hoje`, `amanha`/`amanhã`, `ontem`, e `today`/`tomorrow` | — |
| Dia da semana | `segunda`…`domingo`, ou abreviado: `seg`, `ter`, `qua`, `qui`, `sex`, `sab`, `dom` | sempre o **próximo**, nunca hoje. `@sexta` numa sexta é a que vem |
| `DD/MM` | `@25/12`, e `@25/12/2027` | sem ano e a data já passou, assume o ano que vem |
| ISO | `@2026-12-25` | — |

`ta note "comprar pão @amanha !alta #casa"` cria uma Task com prazo, prioridade e
etiqueta numa tacada. Nenhuma marca é obrigatória — `ta note "ideia solta"`
também funciona, e o que não casa com nenhuma marca fica no texto.

**Você também não precisa de marca nenhuma.** O parser lê data e hora escritas em
português corrente:

```
ta note "Revisar o PR do Thi hoje"                    -> tarefa, prazo hoje
ta note "dentista amanhã"                             -> tarefa, prazo amanhã
ta note "reunião com o cliente na terça às 14h"       -> prazo e lembrete
ta note "ir na nutricionista 17 de setembro às 8:30"  -> vira EVENTO na agenda
```

Diferente das marcas, a expressão em português **não é retirada do texto**: a
frase continua lendo como uma frase. E a marca explícita sempre vence — `@sexta`
num texto que também diz "hoje" resolve para sexta.

Duas guardas contra falso positivo, ambas medidas em cima de exemplos reais:

| Texto | Resultado | Por quê |
|---|---|---|
| `rodar 8h de bateria` | nada | hora solta exige preposição (`às 8h`) |
| `hoje aprendi sobre WAL` | nada | frase retrospectiva não é prazo |
| `hoje eu preciso revisar` | prazo hoje | a guarda não engole prazo legítimo |

### A segunda passada

Logo depois de capturar, o LLM relê a nota em background e pode corrigir o que o
regex não tinha como saber. O regex acerta a **forma** ("hoje" é uma data) e erra
a **intenção** ("hoje aprendi X" é relato); o modelo é o oposto.

Ele pode fazer estas coisas, e **avisa na tela sempre que faz alguma**:

1. **Ajustar ou remover o prazo.** É como "hoje aprendi X" perde o prazo que a
   lista de verbos curada não pegou.
2. **Distinguir tarefa de compromisso.** "Revisar o PR até sexta" é tarefa com
   prazo; "nutricionista 17 de setembro às 8:30" é compromisso com hora de
   início — e vira evento.
3. **Criar o evento na agenda**, na conta certa.

Quem decide a conta são duas camadas: o **LLM julga a categoria** ("trabalho" ou
"pessoal") e o **código mapeia** para uma agenda concreta — e-mail de provedor
pessoal vai para a agenda pessoal, domínio próprio vai para a de trabalho.

Para julgar, o modelo recebe o seu perfil de Priorities (o que o `ta init`
coletou) e os **domínios** das contas — nunca os e-mails completos. É o que
permite "revisar o consumer de Kafka" ir para o trabalho sem a nota dizer isso: o
perfil diz que Kafka é o seu trabalho. Se `ta init` nunca rodou, o modelo decide
só pelo texto, e o padrão na dúvida é **pessoal** — a agenda que colegas não veem.

4. **Etiquetar** nos três eixos abaixo.
5. **Definir a prioridade**, quando o texto e o seu perfil permitirem dizer — e
   **anotação nunca recebe prioridade**, porque registro e ideia solta não são
   cobráveis.

### Os três eixos de tag

| Eixo | Valores | Regra |
|---|---|---|
| **área** | `#trabalho` · `#pessoal` | sempre uma das duas |
| **tipo** | `#tarefa` · `#compromisso` · `#anotacao` | sempre um dos três |
| **tema** | `saude` `casa` `estudo` `financeiro` `compras` `familia` `ideia` | no máximo dois, de vocabulário **fechado** |

O tema é fechado de propósito: se o modelo pudesse inventar tag, cada nota
ganharia um sinônimo quase-igual ("trabalho"/"profissional"/"job") e o
agrupamento visual perderia o sentido — que é o motivo de a tag automática
existir. Você continua livre para escrever `#qualquer-coisa` à mão: a lista
restringe o modelo, não você.

**O que você escreveu vence, com uma exceção deliberada.** `!alta` e `#tag`
digitados travam o campo para sempre — nem uma reetiquetagem futura os toca. Mas
**área e tipo entram mesmo assim**, ao lado das suas tags: eles são estrutura, não
tema, e sem eles a nota fica invisível nos filtros do mural. Isso é pior que estar
mal classificada.

E há uma diferença que importa entre "seu" e "da máquina": o que a revisão pôs
numa passada anterior ela pode **rever** na próxima. Sem isso, `revisar tudo`
nunca conseguiria corrigir uma decisão errada dela mesma.

Medido nesta máquina: **a captura responde em 81ms e a revisão conclui em ~10s.**
Isso **não** atrasa a captura: o `ta note` responde na hora, e a revisão chega
depois. Se você estiver offline, a nota é gravada do mesmo jeito e a revisão
simplesmente não acontece. `TA_AUTO_REVIEW=0` no `.env` desliga a segunda passada
inteira.

⚠️ **A criação de evento não pede confirmação**, por decisão registrada na
[emenda do ADR 0007](docs/adr/0007-propor-e-confirmar-antes-de-escrever-na-agenda.md).
As outras duas guardas continuam de pé: **nunca convidados**, e **só na agenda
dedicada "Terminal Assistant"** — se ela não existir na conta, nada é criado, em
vez de cair para a agenda principal. Um evento indesejado se apaga sozinho; um
convite enviado para um colega, não.

**O dia**

| Comando | O que faz |
|---|---|
| `ta today` | Clima, compromissos das duas agendas e tarefas **ordenadas por prioridade**. Determinístico |
| `ta today --date 2026-08-15` | O mesmo, para outro dia |
| `ta prose` | O mesmo dia em prosa. **Usa LLM**, é opcional |

**Casa**

| Comando | O que faz |
|---|---|
| `ta on <termo> [0-100]` | Liga. O termo pode ser `entity_id`, grupo, apelido ou ambiente |
| `ta off` | Apaga tudo que está aceso |
| `ta off <termo>` | Apaga só aquilo |
| `ta luz <termo> [0-100]` | Igual a `ta on`, mantido pela familiaridade |
| `ta entities` | O inventário, vindo do HA |
| `ta router` | IP externo, download, upload |
| `ta temp` | Temperatura, clima, umidade e consumo da tomada |
| `ta lighter [on\|off\|toggle]` | A borda luminosa. Sem argumento, alterna |
| `ta lighter --profile Meet` | Aplica um profile da Lighter por nome |
| `ta media [entity] [ação]` | Mídia num Echo. Requer `media_player` no HA |

Sobre o brilho: **sem número, `ta on` acende no máximo.** Um padrão menor faria o
comando mais curto ser o mais fraco. Brilho só existe no domínio `light` — num
`switch` ele é ignorado com aviso no log, e o aparelho liga normalmente.

**LLM (Gemini).** Nada disto está no caminho crítico, e tudo o que produz é
gravado:

| Comando | O que faz |
|---|---|
| `ta init` | A entrevista de prioridades (4 perguntas). `--force` refaz |
| `ta priorities` | Mostra as prioridades atuais |
| `ta priorities "<instrução>"` | Reescreve por prompt, ex: `"prioriza estudo acima de casa"` |
| `ta organize` | Agrupa e ordena as notas, e **grava**. Respeita o que você arrastou |
| `ta event "<texto>"` | Detecta um evento no texto e **propõe**. Só grava com o seu sim |
| `ta event --note-id 7` | O mesmo, a partir de uma nota já capturada |

**Automação**

| Comando | O que faz |
|---|---|
| `ta rules` | As regras que o daemon carregou |
| `ta rules check` | Valida os arquivos sem subir o daemon, e mostra o traceback |
| `ta rules check --dir <caminho>` | Valida um diretório qualquer de regras |

**Diagnóstico**

| Comando | O que faz |
|---|---|
| `curl localhost:7777/health` | O daemon leu o `.env`? Reporta *presença* de segredo, nunca o valor |
| `systemctl --user status ta` | O serviço está de pé? |
| `journalctl --user -u ta -f` | O log ao vivo. É onde as regras contam o que fizeram |

Aliases e atalhos globais prontos para colar: [docs/aliases.md](docs/aliases.md).

### Filtros e revisão pelo mural

O cabeçalho tem dois seletores e um botão:

| Controle | O que faz |
|---|---|
| **área** | mostra só `#trabalho` ou só `#pessoal` |
| **tipo** | mostra só tarefas, só compromissos ou só anotações |
| **tag** | mostra só um tema. Lista apenas os temas que existem de fato, e deixa área e tipo de fora porque eles têm seletor próprio |
| **revisar tudo** | devolve **todas as notas abertas** à fila de revisão do LLM |

Com filtro ativo o seletor fica destacado e o contador muda para `3 de 7`, para
"não tem nota" e "o filtro escondeu tudo" nunca se confundirem — e quando o
filtro esconde tudo, aparece um botão de limpar.

**`revisar tudo` pede dois cliques.** O primeiro arma e o botão vira
`confirmar? reetiqueta tudo` por 6 segundos; o segundo executa. Não é um
`confirm()` do navegador de propósito: diálogo modal trava a página e não é
automatizável em teste. A ação custa uma chamada de modelo por nota, então vale
a confirmação — e serve para dois casos: notas capturadas antes de a revisão
existir, e reetiquetar tudo depois de editar as suas Priorities.

**Concluídas, canceladas e apagadas ficam de fora**: revisar prazo de coisa
encerrada é gastar chamada de modelo por nada.

O mesmo pelo terminal: `ta revise` — enfileira e acompanha até drenar.

### Apagar e a lixeira

Cada post-it tem um **`×`** no canto inferior direito, depois do número — o ponto
mais longe do texto e do seletor de estado. A barra do post-it quebra em duas
linhas quando precisa: o seletor de estado sozinho ocupa metade da largura, e sem
quebrar o que não cabia **escapava para fora do card**. Ele **pede dois cliques**: o primeiro
troca o `×` por um `apagar?` vermelho, que volta ao normal sozinho em 4 segundos;
o segundo apaga. E apaga de forma **reversível**: a nota vai para a lixeira, não
some.

A lixeira fica em **`/board#lixeira`**, escondida de propósito: é tela de
recuperação, não parte do fluxo diário, e um botão permanente para "ver o que
apaguei" só ocuparia espaço. Lá o cabeçalho fica escuro e escrito `Mural ·
lixeira`, o `×` vira **`↩`** para restaurar, e a captura fica desabilitada —
escrever ali criaria uma nota que você não veria, porque essa tela só mostra
apagadas.

Pelo terminal: `ta rm <id>`, `ta rm --list`, `ta restore <id>`.

**Apagada e cancelada são eixos independentes.** `cancelled` significa "decidi
não fazer" e continua no kanban; apagada significa "não quero mais ver isto" e sai
de tudo — lista, Digest, lembretes e fila de revisão. Dá para apagar uma nota
concluída, e um sexto `status` perderia essa distinção.

O evento na agenda **não** é removido junto: apagar a anotação sobre um
compromisso não desmarca o compromisso.

### Atualização automática

O mural recarrega sozinho a cada 15s, porque a revisão muda a nota alguns
segundos depois da captura e sem isso você veria o resultado do regex até apertar
F5. Ele **não** recarrega no meio de um arraste, nem enquanto há texto no campo
de captura — recarregar reconstrói o DOM e apagaria o que você está escrevendo.

### As três visões do mural

O mural tem um alternador no topo, e a visão escolhida sobrevive ao reload.

| Visão | Para que serve | O que o arrastar faz |
|---|---|---|
| **geral** | Mural livre. Post-it fica onde a mão deixou | muda a **posição** |
| **lista** | Tudo em lista por prioridade, com borda colorida por prioridade e as terminais no fim | — (o estado muda pelo seletor) |
| **kanban** | Cinco colunas: Não feito, Em andamento, Em hold, Concluída, Cancelada | muda o **Status** |

### Cor do post-it

A cor sai de uma ordem de precedência, do mais específico para o menos:

| # | Origem | Cor |
|---|---|---|
| 1 | **a sua mão** (clique num swatch) | o que você escolheu |
| 2 | **prioridade × área** | a tabela abaixo |
| 3 | área sozinha, sem prioridade | `#trabalho` lilás `#dcd8f0` · `#pessoal` menta `#d3ead8` |
| 4 | nada disso | papel neutro `#efefe6` |

**Prioridade escolhe a família, área desloca o tom.** Trabalho puxa para o frio,
pessoal para o quente:

| | `#trabalho` | `#pessoal` |
|---|---|---|
| `!alta` | `#fbc1c4` rosa | `#ffc5b3` salmão |
| `!media` | `#f6e8b0` areia | `#ffe7a1` âmbar |
| `!baixa` | `#cfd5e4` cinza-violeta | `#cadddb` cinza-verde |

Os seis são fixos e **medidos em CIELAB**, entre ΔE 8 e 11 um do outro: visíveis
lado a lado sem deixar de ser a mesma prioridade. Derivar por regra única não
funcionou — as três famílias têm saturações muito diferentes, então misturar a cor
de área ficava imperceptível no cinza (ΔE 5,8) e exagerado no âmbar, e girar o
matiz tinha o problema inverso.

A **paleta manual** sai do mesmo sistema, para uma escolha sua não parecer de
outro app: `#ffe7a1` âmbar · `#ffc5b3` salmão · `#fbc1c4` rosa · `#d3ead8` menta
· `#dcd8f0` lilás · `#efefe6` neutro. A menor distância entre duas quaisquer é
ΔE 10.

⚠️ Nota colorida à mão **antes** desta paleta mantém a cor antiga, e nenhum swatch
aparece marcado. O botão `A` a traz para o sistema novo.

Na **visão em lista** a prioridade aparece também como borda esquerda, em tom
forte: `#c0392b` alta · `#d68910` média · `#7f8c8d` baixa. Ali os dois eixos
convivem — fundo mostra a área, borda mostra a urgência.

As matizes de área são bem afastadas das de prioridade porque os dois eixos são
perguntas diferentes: prioridade é *quão urgente*, área é *de que parte da vida*.

A cor de área aparece justamente onde ela é útil: em **anotação**, que por
definição não tem prioridade, e em compromisso sem urgência declarada.

Clicar num swatch grava a cor e ela passa a vencer o padrão para sempre — mesmo
que a prioridade mude depois. O botão **`A`** de cada post-it limpa a escolha
manual e devolve a nota ao automático.

No banco isso é um campo só: `color` nulo significa "usa o padrão da prioridade",
e a derivação acontece na hora de desenhar. Por isso mudar a prioridade recolore
uma nota nunca tocada e não recolore uma que você pintou.

Os cinco estados são um eixo próprio, independente dos papéis: uma Note pode ser
Task (tem prazo) e estar em hold. `Concluída` e `Cancelada` são terminais — saíram
da fila por caminhos diferentes, e cancelar nunca grava data de conclusão. Ver a
emenda no [ADR 0006](docs/adr/0006-uma-note-com-papeis.md).

O arrastar usa **pointer events**, não a API de drag-and-drop do HTML5, para
funcionar em toque — o mural é responsivo justamente para abrir no celular.

## Automações

É a parte que motivou o projeto: **combinar o estado do PC com o estado da casa.**
Nenhum motor pronto resolve isso, porque o Home Assistant não sabe que você entrou
numa call e o GNOME não sabe acender uma lâmpada.

Uma automação é uma **Rule**: gatilho, condição opcional, ações. Vive num arquivo
Python em `rules/`, versionado junto do código.

```python
# rules/reuniao.py — a regra original, resumida
@rule(on=mic_active(), when=after("16:00"))
async def reuniao_tarde(ctx):
    evento = await ctx.calendar.agora()          # a agenda diz QUAL reunião é
    if "1:1" in (evento or {}).get("summary", ""):
        return                                   # 1:1 não precisa de produção
    await ctx.home.switch_on("light.lampada_do_quarto", 100)
    await ctx.lighter.apply_profile("Meet")
```

Os gatilhos disponíveis hoje: microfone entrando e saindo de uso, hora do dia,
Reminder vencendo, e mudança de estado de qualquer entity do HA. As ações alcançam
a casa, o ringlight, a agenda e as notificações.

**O guia completo, com referência de API e receitas prontas para copiar, está em
[docs/automacoes.md](docs/automacoes.md).** As receitas dele são validadas por
teste — se uma parar de carregar, o `pytest` acusa.

Depois de editar uma regra: `ta rules check` valida, `systemctl --user restart ta`
põe no ar. O daemon **não** recarrega sozinho.

## Princípios que o código respeita

Três invariantes que não são configuração e não devem ganhar um flag que as
desligue:

1. **O LLM nunca está no caminho crítico.** Capturar uma nota e abrir o mural
   funcionam offline, sempre. Quando o modelo roda sozinho — a segunda passada
   sobre a captura — é **depois** da resposta, nunca antes, e o que ele produz é
   gravado — então nada se reorganiza sozinho, e sua mão sempre vence.
   ([ADR 0003](docs/adr/0003-llm-fora-do-caminho-critico.md))
2. **Nada é escrito na agenda sem confirmação, e nunca com convidados.** O modo de
   falha é um compromisso fantasma visível para colegas de trabalho.
   ([ADR 0007](docs/adr/0007-propor-e-confirmar-antes-de-escrever-na-agenda.md))
3. **A calibração da borda luminosa mora na Lighter**, não aqui. O daemon manda no
   *quê* (qual profile aplicar); a extensão manda no *como*.

## A extensão Lighter

O ringlight é a extensão do GNOME [Lighter](https://github.com/joaoferrete/Lighter),
mantida no repositório `~/Lighter`. O daemon a comanda por `gsettings` — controle
externo que já funciona sem alteração — e ela recebeu um listener de
`changed::active-profile` para que aplicar um profile por nome fosse possível de
fora.

Editar sempre `~/Lighter`, nunca a cópia em
`~/.local/share/gnome-shell/extensions/`, que é derivada. O fluxo é `make lint` →
`make pack` → `make install`, e **no Wayland é preciso logout/login** para
recarregar o GNOME Shell; `make nested` roda uma sessão aninhada para testar antes.

## Documentação

- [`CONTEXT.md`](CONTEXT.md) — o glossário. Vale ler antes de nomear qualquer coisa
  nova, principalmente pela desambiguação entre *Lighter profile* e *Priorities*.
- [`docs/adr/`](docs/adr/) — por que as decisões difíceis foram tomadas assim,
  incluindo as opções rejeitadas que alguém tentaria "consertar" depois.
- [`docs/automacoes.md`](docs/automacoes.md) — **como escrever automações**: o
  modelo, todos os gatilhos e condições, a referência do `ctx`, seis receitas
  prontas e os limites de hoje.
- [`docs/aliases.md`](docs/aliases.md) — aliases e atalhos globais.
- [`rules/`](rules/) — as automações. São Python, versionadas de propósito.

## Quando algo não funciona

**O primeiro `ta today` depois de um restart demora ~8s** e avisa que a agenda
estava aquecendo. É esperado: o Evolution tem 8 fontes, e cada agenda do Google
consome um tempo de espera fixo na conexão. As chamadas seguintes levam 0,04s.

| Sintoma | Onde olhar |
|---|---|
| `import gi` falha | O venv foi criado com o Python errado. Recriar com `/usr/bin/python3 -m venv --system-site-packages` |
| `ta today` não lista nada | As contas Google não estão conectadas no GNOME, ou o EDS ainda não sincronizou |
| `ta luz` não responde | Daemon ou Home Assistant fora do ar: `systemctl --user status ta`, `docker ps` |
| A borda não acende | Conferir `gsettings get org.gnome.shell.extensions.lighter enabled` e se a extensão está ativa |
| Atalho de teclado sumiu | A lista `custom-keybindings` foi sobrescrita — ver a armadilha 1 em [docs/aliases.md](docs/aliases.md) |
| Automação disparou na hora errada | `journalctl --user -u ta -f`. Só existe um lugar para olhar, e isso é de propósito ([ADR 0002](docs/adr/0002-motor-de-automacao-proprio-em-python.md)) |
