# Terminal Assistant

Assistente pessoal de linha de comando para uma única máquina, reunindo o
controle da casa inteligente e a captura de anotações do dia sob a mesma
ferramenta. O que une os dois lados é um motor de gatilhos compartilhado.

Os termos são grafados em inglês porque o vocabulário do Home Assistant é em
inglês e ele é a fonte de verdade do inventário da casa. As definições ficam em
português.

## Language

### Casa

**Device**:
Um aparelho físico da casa — uma lâmpada, uma tomada. Existe no mundo, tem
marca e protocolo.
_Avoid_: dispositivo, gadget, coisa

**Entity**:
A menor unidade controlável ou observável exposta pelo Home Assistant. Um único
Device pode expor várias Entities, e é sempre uma Entity que um comando
endereça — nunca um Device.
_Avoid_: entidade, item, recurso

### Automação

**Trigger**:
A condição que dá início a uma automação. Pode nascer do tempo, de um sinal da
própria máquina ou da mudança de estado de uma Entity.
_Avoid_: evento, disparo, hook

**Rule**:
Um Trigger, suas condições e suas ações, tomados como uma unidade. É a forma que
uma automação assume quando escrita.
_Avoid_: automação, receita, cena

### Notas

**Note**:
A unidade de captura: um texto que o usuário registrou. É a única entidade do
módulo de notas — os papéis que ela assume vêm da presença de atributos, não de
um tipo escolhido na captura.
_Avoid_: item, card, entrada

**Task**:
O papel que uma Note assume quando ganha prazo. É cobrável e pode ser concluída.
Não é uma entidade separada.
_Avoid_: to-do, pendência

**Reminder**:
O papel que uma Note assume quando ganha um instante de disparo. É consumido
quando dispara. Não é uma entidade separada.
_Avoid_: alarme, aviso

**Status**:
O estado de uma Note na fila de trabalho: `todo`, `doing`, `hold`, `done` ou
`cancelled`. É um eixo próprio, independente dos papéis — uma Note pode ser Task
e estar em `hold`. `done` e `cancelled` são terminais: a Note saiu da fila, por
caminhos diferentes.
_Avoid_: estado, situação, coluna

**Post-it**:
A representação visual de uma Note no quadro. É desenho, nunca dado — nada é
"um Post-it" no modelo.
_Avoid_: usar como sinônimo de Note

**Priorities**:
A descrição, mantida pelo próprio usuário, do que importa para ele: trabalho,
o que é relevante, o que vem antes. É o que dá sentido à palavra "prioridade"
quando uma Note é ordenada.
_Avoid_: perfil, preferências, contexto

**Digest**:
A visão consolidada de um dia, reunindo compromissos da agenda e Notes
cobráveis.
_Avoid_: resumo, briefing, agenda do dia

### Desambiguação

**Profile** é ambíguo neste projeto e não deve ser usado sozinho. A extensão
[Lighter](https://github.com/joaoferrete/Lighter) chama de _profile_ um conjunto de valores de aparência da borda
luminosa. A descrição do que importa para o usuário é **Priorities**. Quando o
sentido da Lighter for necessário, escrever _Lighter profile_.
