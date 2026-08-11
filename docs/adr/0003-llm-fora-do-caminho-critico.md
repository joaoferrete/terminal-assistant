# O LLM nunca fica no caminho crítico

Despejar um pensamento não pode esperar rede, mas organizar o quadro pode. A
captura de uma Note é sempre determinística, local e instantânea: sintaxe leve
resolve tag, prioridade e prazo. O LLM entra apenas sob comando explícito, e o
que ele produz — agrupamento e ordem — é **gravado** como dado. Abrir o quadro
depois nunca chama o modelo.

## Consequences

- Sem internet, ou sem chave de API, a ferramenta continua servindo para capturar
  e consultar. Só o luxo de reorganizar fica indisponível.
- A ordem que o usuário estabelece à mão sobrevive: por ser dado gravado, e não
  cálculo repetido a cada abertura, um `organize` posterior não desfaz o que foi
  arrastado a menos que se peça.
- Duas aberturas seguidas do quadro mostram sempre a mesma coisa. Nada se
  reorganiza sozinho, e nada custa dinheiro por ser olhado.
  ⚠️ **A primeira frase deixou de valer** — ver a emenda abaixo.

## Emenda, 2026-08-11: o relógio pode reorganizar; o modelo não

A terceira consequência acima dizia que duas aberturas seguidas do quadro mostram
sempre a mesma coisa. O ADR 0010 quebra isso de propósito: a ordem passou a
começar pelo Horizon, a faixa de tempo em que o prazo cai, e essa faixa é
derivada do relógio na hora de exibir. À meia-noite, uma Note que vencia amanhã
passa a vencer hoje e sobe — sem ninguém pedir.

A frase estava larga demais. O que este ADR decide, e continua de pé, é que **o
LLM** e **a rede** não ficam no caminho crítico. O relógio não é nenhum dos dois:
é local, instantâneo, grátis e determinístico dado o dia. E um quadro em que a
tarefa de hoje não sobe no dia de hoje é um quadro que mente — negar isso para
preservar a frase seria preservar a letra e perder o motivo.

O que sobrevive intacto, e é a parte que importava:

- Abrir o quadro **nunca** chama o modelo, nem custa dinheiro.
- Sem rede, a ordem continua correta: o horizonte é calculado na máquina.
- **Nada é sobrescrito no banco** pela passagem do tempo. `sort_key` só muda
  quando você arrasta ou pede `organize` — e é por isso que a faixa é derivada em
  vez de gravada.
- O arraste continua ganhando do modelo, pelo mesmo `pinned_by_user`.
