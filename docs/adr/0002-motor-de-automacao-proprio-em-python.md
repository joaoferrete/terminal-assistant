# Motor de automação próprio, escrito em Python

O Home Assistant tem motor de automação, mas não vê o estado da máquina do
usuário — e todo gatilho que motivou este projeto nasce ali (microfone em uso,
hora do dia, agenda local). Como o estado do PC teria que ser empurrado para
dentro do HA de qualquer forma, o app passa a ser o único cérebro: ele avalia os
gatilhos e trata o HA como atuador burro. As Rules são escritas em Python, com
decorators, e vivem versionadas no repositório.

## Considered Options

- **O Home Assistant como cérebro**, recebendo o estado do PC e chamando o app de
  volta. Rejeitado: as Rules sairiam do repositório para o YAML do HA, e uma ação
  local passaria a fazer o trajeto PC → HA → PC.
- **Divisão por domínio** — casa no HA, PC no app. Rejeitado: duas cabeças
  significam dois lugares para investigar quando uma luz acende na hora errada, e
  a condição "e passou das 16h" não existe no lado da [Lighter](https://github.com/joaoferrete/Lighter).
- **Rules declarativas em YAML.** Rejeitado por custo de implementação: com
  Python o motor é um despachante de eventos; com YAML seria necessário escrever
  um interpretador — parser, avaliador de condição e, inevitavelmente, uma
  linguagem de template — antes da primeira Rule rodar. As automações do próprio
  Home Assistant são o exemplo de onde esse caminho termina.

## Consequences

- Erro de sintaxe num arquivo de Rule pode derrubar o daemon. Mitigado carregando
  cada Rule isoladamente e com um comando de verificação estática.
- As Rules não são editáveis por interface gráfica, e essa troca foi deliberada:
  o motivo de o app ser o cérebro era justamente tê-las sob controle de versão.
