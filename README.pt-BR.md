# Terminal Assistant — resumo em português

> **Isto é um resumo, não uma tradução.** A documentação completa está em inglês,
> e é sempre a versão correta. Esta página é curta de propósito: pequena o
> bastante para nunca ficar desatualizada.

Um motor de gatilhos para as suas tarefas e para a sua casa, num comando só.

Capture uma nota em um segundo e não pense mais em onde ela foi parar. Deixe o
relógio decidir o que aparece primeiro. E, se quiser, deixe **o estado da sua
própria máquina** comandar as luzes — porque o Home Assistant não sabe que você
entrou numa call, e o GNOME não sabe acender uma lâmpada.

**O núcleo roda em qualquer lugar que rode Python.** Notas, mural, motor de
gatilhos e scheduler não precisam de mais nada. O resto — Home Assistant, agenda,
microfone, IA — é opcional, e cada integração confere se consegue funcionar e fica
quieta se não conseguir.

## Começando

```bash
git clone https://github.com/joaoferrete/terminal-assistant
cd terminal-assistant
make install

ta doctor                                    # o que funciona na sua máquina
ta note "ligar pro dentista @sexta !alta"    # captura
ta list                                      # vencidas primeiro, depois hoje
ta board                                     # o mural, no navegador
```

`ta doctor` é o comando que responde tudo: quais integrações estão vivas aqui, por
que as outras não estão, e o comando exato para consertar cada uma.

Também dá para rodar num **servidor doméstico** sempre ligado, com o notebook
fechado. Nesse caso você abre mão da agenda, do microfone e da ringlight, que
dependem do desktop. O passo a passo está em
[Install → on a home server](docs/install.md#alternative-on-a-home-server).

## Idioma

A ferramenta segue o locale do sistema, então numa máquina em português ela já
fala português — captura, interface e o idioma em que o modelo responde.

```bash
ta lang            # mostra o idioma e DE ONDE ele veio
ta lang pt
systemctl --user restart ta
```

Um idioma por vez, e isso é decisão: `@03/04` é 3 de abril em português e 4 de
março na convenção americana. Aceitar os dois faria esse token devolver uma data
válida e errada, em silêncio, para metade das pessoas.

O idioma é da **instalação**, não da chamada — quem interpreta a nota é o daemon,
então `TA_LANG=en ta note ...` não muda nada.

## A ordem do quadro

O que ordena é o **prazo**, não a prioridade que você declarou. Cada tarefa cai
numa faixa contada a partir de hoje — vencida, hoje, próximos 7 dias, depois — e
a prioridade ordena **dentro** da faixa.

Então uma `!baixa` que vence hoje fica acima de uma `!alta` que vence em três
dias: a de hoje é a que precisa ser feita hoje.

`!alta`, `!media` e `!baixa` continuam valendo para sempre na captura, ao lado das
palavras em inglês.

## Segurança, em uma linha

O daemon escuta só em `127.0.0.1`, e **recusa subir** num endereço que a rede
alcança sem um `TA_TOKEN` — porque ele expõe todas as suas anotações e a
credencial que comanda a sua casa. Abrir o mural no celular são duas variáveis no
`.env`.

## A documentação completa

Está em inglês, e é onde estão as respostas de verdade:

| | |
|---|---|
| [Instalação](docs/install.md) | Passo a passo, em camadas independentes |
| [Configuração](docs/configuration.md) | Toda variável e chave |
| [Quando não funciona](docs/troubleshooting.md) | Sintoma → causa → conserto |
| [Home Assistant](docs/home-assistant.md) | Do zero: token, entities, apelidos, armadilhas |
| [Notas e mural](docs/notes.md) | Sintaxe de captura, papéis, ordem, as três visões |
| [Automações](docs/automations.md) | O motor de regras e seis receitas |
| [IA](docs/ai.md) | O que acrescenta, o que custa, como desligar |

Os **ADRs** em [`docs/adr/`](docs/adr/) explicam por que as decisões difíceis
foram tomadas assim, incluindo as alternativas rejeitadas. Eles estão em
português, e são a parte mais interessante do repositório.

## Licença

[Apache-2.0](LICENSE). Contribuições entram sob a mesma licença pela §5 dela, então
não há CLA para assinar.
