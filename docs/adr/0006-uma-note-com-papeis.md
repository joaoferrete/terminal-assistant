# Uma única entidade Note, com papéis dados por atributo

O módulo de notas existe para ser um lugar onde se escreve sem cerimônia, então a
captura nunca pergunta o tipo do que está sendo escrito. Há uma entidade só,
`Note`, e os papéis emergem da presença de atributos: com prazo ela é Task, com
instante de disparo é Reminder, sem nenhum dos dois é só texto que fica. O parser
e, sob comando, o LLM promovem a Note depois — nunca durante a escrita.

## Considered Options

- **Task, Reminder e Note como entidades separadas**, cada uma com seu ciclo de
  vida. É o modelo mais correto — "concluída" não faz sentido numa ideia solta, e
  o disparo de um Reminder não é um campo opcional pendurado. Rejeitado porque
  obriga a escolher o tipo na captura, ou a deixar o LLM escolher e errar, e as
  duas coisas atritam com o motivo de o módulo existir.
- **Uma entidade sem vocabulário de papéis**, em que tudo é chamado de nota.
  Rejeitado: o Digest ficaria sem hierarquia, porque nada distinguiria o que é
  cobrável do que é só texto.

## Emenda, 2026-08-10: Status é um eixo à parte dos papéis

A visão em kanban pediu cinco estados — não feito, em andamento, em hold,
concluída, cancelada — e `done_at IS NOT NULL` só sabia responder sim ou não.
`cancelled` é o caso que prova a falta: uma Note cancelada saiu da fila **sem ter
sido feita**, e o modelo binário não tinha como dizer isso.

A Note ganhou uma coluna `status` com esse enum. Isso **não** substitui os papéis
derivados por atributo: `due` continua fazendo dela uma Task e `remind_at` um
Reminder, e uma Note pode perfeitamente ser Task e estar em `hold`. São dois eixos
ortogonais, e a decisão de não ter entidades separadas por tipo permanece intacta.

`done_at` também não foi substituído: continua sendo o **instante** em que a Note
entrou em `done`. Estado e carimbo de tempo são coisas diferentes, e cancelar
nunca grava `done_at`.

## Consequences

- Um único schema e uma única superfície de leitura.
- A interface e o CLI ainda falam com precisão — "2 tarefas vencem hoje", não
  "2 notas" — porque o vocabulário de papéis existe no glossário mesmo sem existir
  como tipo no banco.
- Combinações estranhas são representáveis (uma Note concluída sem prazo, por
  exemplo). Cabe à interface não oferecê-las, já que o modelo não as impede.

## Emenda, 2026-08-11: o valor gravado é canônico; idioma é entrada e exibição

A prioridade era o **único enum em português** do schema (`alta|media|baixa`),
convivendo com um `status` que sempre foi inglês (`todo|doing|hold|done|
cancelled`). Conviveram bem enquanto nada pedia nada ao modelo em inglês.

Levar `TA_LANG` ao LLM transforma isso em bug de verdade. Instruído a responder
em inglês, o modelo devolve `"high"`; a validação da revisão recusa o valor por
não estar no enum; e a prioridade some **em silêncio** — sem erro, sem log. A
nota volta da revisão sem prioridade e ninguém entende por quê.

A regra que resolve, e que passa a valer para todo campo estruturado:

> **O valor gravado é canônico e único. Idioma é coisa de entrada e de exibição.**

Na prática:

- O banco guarda `high|medium|low`, alinhado com `status`. Quem abre o SQLite lê
  uma língua só.
- `!alta`, `!media`, `!baixa` continuam aceitos na captura **para sempre**. Foram
  a sintaxe por toda a vida do projeto, estão na memória muscular de quem usa, e
  quebrá-los não compraria nada. `!high` também vale.
- A tela e o CLI mostram no idioma do usuário.
- O que o modelo devolve em campo **estruturado** é canônico, independente do
  idioma em que ele foi instruído a escrever a prosa.

A migração 6 reescreveu os valores existentes. É a primeira migração do projeto
que altera **dado** em vez de acrescentar estrutura, e por causa disso `connect()`
passou a copiar o banco antes de aplicar qualquer migração pendente: migração é
atômica, mas atômico não é reversível, e o arquivo tem notas que a pessoa
escreveu.

Não vale como precedente para traduzir o resto por gosto. Vale porque havia uma
falha concreta, silenciosa, e um caminho de entrada que preserva o que já se
digitava.
