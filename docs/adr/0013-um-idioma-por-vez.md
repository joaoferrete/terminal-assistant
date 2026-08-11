# Um idioma por vez, e não os dois ao mesmo tempo

A ferramenta precisa falar inglês além de português: o repositório vai a público,
e a interface, as mensagens e o parser de captura eram todos só em português.

O caminho óbvio seria aceitar os dois vocabulários simultaneamente — `@sexta` e
`@friday`, `!alta` e `!high`, os dois sempre válidos. É o que parece mais
generoso, e funciona para quase tudo.

Quase.

> **`@03/04` é 3 de abril em português e 4 de março na convenção americana.**

Com os dois idiomas ativos, esse token é genuinamente ambíguo, e qualquer lado
escolhido está **silenciosamente errado** para metade dos usuários. Não dá erro,
não fica sem marca, não some do texto: devolve uma data **válida e errada**, e a
pessoa perde o prazo sem nunca saber por quê.

Este projeto trata esse modo de falha como categoria, não como caso isolado — o
`@25/12` que era descartado em silêncio, o `home.light()` que respondia 200 sem
fazer nada, a prioridade que sumia na revisão. Todos custaram tempo
desproporcional justamente por não darem erro.

E não dá para contar com a segunda passada do LLM para corrigir: a IA é opcional
por decisão explícita (ADR 0003). O regex é o piso.

Então há **um idioma ativo por vez**, resolvido por `TA_LANG`, e a ordem da data
numérica é consequência dele.

## Precedência

    TA_LANG  →  ~/.config/ta/config.toml  →  locale do SO  →  'en'

O locale vem antes do padrão de propósito: numa máquina em `pt_BR.UTF-8` nada
muda para quem já usa, sem ninguém configurar nada. É também o que dispensa
perguntar o idioma na primeira execução — e a pergunta **não podia** morar no
`ta init`, que é a entrevista de Priorities e exige LLM.

## Considered Options

- **Os dois vocabulários juntos, com `DD/MM` sempre vencendo.** Nada quebra para
  quem já usa e o código fica mais simples. Rejeitado por aceitar conscientemente
  que um americano marque a data errada sem perceber.
- **Os dois juntos, recusando a data numérica ambígua** (quando ambos os números
  são ≤ 12, a marca volta para o texto, como `@casa` já faz). Elegante, honesto,
  e nunca erra calado. Rejeitado porque quebraria `@03/04` para quem já usa — e
  metade das datas curtas cai nessa faixa.
- **Detectar o idioma de cada nota pelo texto.** Mais esperto no papel. Rejeitado
  porque detecção em texto curto ("reunião 03/04") é pouco confiável, e erra em
  silêncio: o mesmo defeito, com mais código para mantê-lo.
- **Só ISO, e acabou.** `@2026-04-03` nunca é ambíguo. Rejeitado porque a sintaxe
  curta é metade do valor da captura rápida.

## Consequences

- **O idioma é da instalação, não da invocação.** `TA_LANG=en ta note "..."` não
  muda como a nota é lida: o CLI é cliente fino e quem faz o parsing é o daemon,
  que resolveu o idioma no boot. Isso é o comportamento correto, não uma
  limitação — se variasse por chamada, duas notas capturadas no mesmo dia leriam
  `@03/04` como datas diferentes e o banco guardaria as duas como se fossem a
  mesma coisa. `ta lang <pt|en>` grava e diz que exige restart.
- **As palavras relativas valem sempre.** `@today` numa máquina em português é
  inequívoco, e recusá-lo seria pedantismo. O que não pode valer nos dois é a
  **data numérica**, e só ela.
- **`!alta` e `!high` são ambos aceitos**, porque prioridade é um enum fechado,
  sem colisão possível. Idioma de entrada não é idioma de gravação (emenda do
  ADR 0006).
- **As formas de data são estruturalmente diferentes, não traduzidas.** Português
  diz "17 de outubro" e "na sexta"; inglês diz "October 17", "17 October",
  "October 17th" e "next Friday". Cada idioma tem o seu conjunto de regex,
  compilado uma vez.
- **AM/PM só existe em inglês, e obrigou uma regra nova.** Antes, `8pm` não casava
  **nada** — o lookahead falhava no `p` e a marca sumia em silêncio. Para aceitar
  `8pm`, o sufixo de minutos teve de virar opcional, e isso fez o dia do mês ser
  relido como hora: "dentist on October 17" ganhava lembrete às 17:00. A guarda —
  número pelado nunca é hora — passou a ser explícita.
- **A lista de verbos retrospectivos tem de existir em cada idioma.** "hoje
  aprendi X" e "today I learned X" são registro, não tarefa, e o regex acerta a
  forma e erra a intenção sem essa curadoria.
- **O catálogo de mensagens é injetado no HTML do mural**, não buscado por uma
  rota: o idioma não muda durante a vida da página, e uma segunda requisição só
  acrescentaria latência e um instante desenhando chaves cruas. O JS não tem
  tabela paralela — mesma disciplina do `HORIZON_LABEL`.
- **Um teste garante que os dois idiomas têm exatamente as mesmas chaves.** Sem
  ele, uma chave só em `pt` vira texto errado na máquina de quem usa `en`, e só
  lá.
- **O `lru_cache` do idioma virou fonte de acoplamento entre testes.** Um teste
  que fixa `TA_LANG=en` deixava o cache assim para os seguintes, e treze testes
  de parser em português passaram a falhar sem que nada relacionado a eles
  tivesse mudado. Um `conftest` autouse limpa os caches em toda volta.
- **O corpo dos prompts segue em português.** O que muda com `TA_LANG` é o idioma
  em que o modelo **responde**, via instrução de sistema num lugar só. Reescrever
  os prompts mudaria o comportamento do modelo sem que houvesse como comparar
  antes e depois sem queimar chamadas — e não é isso que a decisão precisa.
