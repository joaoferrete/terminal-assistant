# O microfone em uso é o gatilho de reunião; a agenda é o contexto

"Estou em reunião" é detectado pelo microfone em uso, observado via PipeWire. A
agenda local entra em seguida como enriquecimento e como condição: ela diz qual
compromisso é, e permite Rules que distinguem tipos de reunião.

A razão de não usar título de janela é do ambiente: a sessão é Wayland, e nenhum
processo externo ao compositor consegue ler o título da janela em foco. O
microfone, além de ser observável, é um sinal universal — vale para Meet, Zoom,
Slack e Discord sem saber a diferença entre eles.

## Considered Options

- **Somente a agenda.** Rejeitado: erra nos dois sentidos, disparando em reunião
  que não aconteceu e ficando cego para chamadas ad-hoc.
- **Título da janela, por extensão do GNOME Shell.** Tecnicamente possível — a
  extensão [Lighter](https://github.com/joaoferrete/Lighter) já faz isso, porque roda dentro do Shell — mas cobriria apenas
  reunião no navegador e acrescentaria uma superfície a manter.

## Consequences

- Há falso positivo previsível: gravar um áudio ou usar o microfone para qualquer
  outra coisa dispara a automação. É aceitável porque a ação é reversível e
  barata.
- A capacidade de ler título de janela continua disponível pela Lighter, caso um
  dia um gatilho mais preciso seja necessário.
