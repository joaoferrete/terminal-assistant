# O relógio ordena o quadro, e a prioridade ordena dentro da faixa

A ordem de exibição das Notes era prioridade primeiro, prazo como desempate. O
efeito era um quadro que não respondia à única pergunta que se faz a ele de
manhã: uma `!alta` com três semanas de folga ficava acima de uma `!media` que
vencia hoje, porque o eixo declarado vencia o eixo real.

Entra o **Horizon**: a faixa de tempo em que o prazo cai, contada de hoje —
`vencida`, `hoje`, `semana` (7 dias, janela rolante) ou `depois`. Ele é o primeiro
critério, à frente da prioridade. A prioridade não perdeu valor, mudou de escopo:
ordena **dentro** da faixa.

Duas consequências disso são decisões, não detalhes de implementação:

**O Horizon é derivado na hora de exibir, nunca gravado.** Ele é função do
relógio — a mesma Note com prazo `2026-08-20` é `depois` hoje e `hoje` no dia 20 —
e nada em `sort_key` pode representar isso.

**E é derivado no servidor.** `GET /notes` devolve as Notes já na ordem de
exibição, cada uma com sua faixa. O mural não faz aritmética de data nem
reordena: as três visões, o `ta list` e o `ta export` recebem a mesma ordem sem
cada um reimplementá-la. Foi o que apagou a terceira cópia da tabela de rank de
prioridade — havia uma em `store.py`, uma em `cli.py` e uma em `board.html`.

## Considered Options

- **Um score contínuo de urgência**, combinando dias até o prazo com a prioridade
  numa única nota. Ordena bem e não explica nada: não existe divisória para
  desenhar, o usuário não consegue prever onde uma Note vai cair, e um teste sobre
  ele fixa um número em vez de uma frase. Rejeitado por ser inauditável, não por
  ordenar errado.
- **A prioridade podendo subir um degrau de faixa**, para uma `!alta` de amanhã
  empatar com as de hoje. Rejeitado por um motivo visual: o mural desenha
  divisórias por faixa, e essa regra colocaria um post-it debaixo de uma divisória
  à qual ele não pertence. Uma ordem que a própria tela contradiz é pior que uma
  ordem mais grosseira.
- **`sem prazo` numa faixa própria, no fim.** Mais honesto sobre o que não tem
  data, e rejeitado porque afunda: uma tarefa `!alta` sem prazo ficaria abaixo de
  uma `!baixa` que vence em setembro. O que não tem data marcada não é, por isso,
  menos importante que o futuro distante — então `sem prazo` mora em `depois`, e o
  rótulo da divisória admite isso: **"depois e sem prazo"**.
- **Um job noturno assando a faixa em `sort_key`**, para manter a ordem como dado
  gravado. Rejeitado duas vezes: brigaria com `pinned_by_user`, reescrevendo à
  noite exatamente o que o usuário arrastou de dia, e destruiria a garantia que o
  ADR 0003 vende — nada é sobrescrito no banco por causa da passagem do tempo.
- **Usar `remind_at` como prazo quando não há `due`.** Tentador: um lembrete às
  22:00 de hoje é claramente "hoje". Rejeitado porque muda o que `vencida`
  significa — um Reminder não está atrasado, ele **já disparou** — e isso
  arrastaria `fired_at` para dentro da ordenação só para lembretes consumidos não
  ficarem morando em `vencida` para sempre.

## Consequences

- **O quadro se reordena sozinho à meia-noite.** É o ponto, e contradiz uma
  consequência escrita no ADR 0003 — ver a emenda lá.
- **O `organize` virou um refinador dentro da faixa.** O que o LLM grava em
  `sort_key` é o último desempate da chave, então a ordem dos grupos que ele
  devolve só aparece entre Notes que já empataram em faixa, prioridade e prazo. O
  prompt foi reescrito para dizer isso: o modelo agrupa por tema e ordena pelo que
  desbloqueia o quê — o relógio é dono do prazo. O agrupamento temático sobrevive
  no campo `group`, que o mural ainda não desenha.
- **A visão `geral` mostra pouco disso.** Ela é a visão default e usa a posição
  gravada; a ordem só aparece na grade de fallback, para Note nunca arrastada.
  Correto pelo ADR 0003 — a mão vence —, mas quem organizou o mural à mão precisa
  olhar a visão **lista** para ver a mudança.
- **Uma `!alta` sem prazo cai abaixo de toda `!baixa` desta semana.** É a mesma
  moeda da escolha de juntar `sem prazo` com `depois`, e é inerente a "o prazo
  domina".
- **`ta export` reordena de um dia para o outro**, porque ordena contra
  `date.today()`. Aceitável num despejo; se um dia o export for versionado,
  agrupar por cabeçalho de faixa resolve.
