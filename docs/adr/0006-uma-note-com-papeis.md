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
