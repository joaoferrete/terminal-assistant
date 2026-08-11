# Propor e confirmar antes de escrever na agenda

Quando o LLM conclui que uma Note descreve um evento, o app mostra título, data,
hora e agenda de destino, e não cria nada sem um sim. Mesmo confirmado, o evento é
gravado numa agenda dedicada — uma camada própria no Google Calendar, colorível e
descartável — e o app **nunca adiciona convidados**, portanto nunca notifica
ninguém.

Isso é deliberadamente menos fluido do que seria possível, e alguém que leia o
código vai querer "consertar" tornando a criação automática. O modo de falha
justifica a fricção: um palpite errado de data escrito direto na agenda de trabalho
vira um compromisso fantasma que colegas veem, e que só se descobre quando alguém
pergunta que reunião é aquela.

## Emenda, 2026-08-10: são duas agendas dedicadas, e o roteamento é do LLM

Existe uma `Terminal Assistant` em **cada** conta — pessoal e trabalho. O evento
detectado é roteado para a que corresponde ao contexto da nota, e essa escolha é
do LLM.

Isso parece contradizer a opção "auto na pessoal, confirmação na de trabalho" que
foi rejeitada acima, mas não contradiz: lá o roteamento decidia entre uma camada
dedicada e uma agenda **real**, e por isso um erro de roteamento derrubava a
proteção. Aqui os dois destinos são camadas dedicadas e descartáveis, então errar
o roteamento põe o evento na camada errada — nunca num compromisso que colegas
veem. A trava permanece intacta, e o ganho é manter o contexto de trabalho junto
do trabalho.

A confirmação continua obrigatória, e a proibição de convidados continua absoluta.

## Consequences

- Se algo escapar, deletar a camada dedicada limpa tudo sem tocar em compromissos
  reais.
- A regra de nunca adicionar convidados é uma invariante do projeto, não uma
  configuração — não deve ganhar um flag que a desligue.
- O schema de saída estruturada da detecção de evento ganha um campo de conta
  alvo, e ele é validado contra as agendas que existem de fato — nunca aceito
  como texto livre.

## Emenda — 2026-08-10: confirmação dispensada, por decisão do usuário

O usuário pediu que uma nota como "ir na nutricionista 17 de setembro às 8:30"
virasse compromisso **sozinha**, e escolheu explicitamente a opção de o LLM criar
o evento sem confirmação, depois de eu apresentar o risco: um evento fantasma na
agenda de trabalho aparece como ocupado para os colegas de trabalho.

**A confirmação cai. As outras duas guardas ficam**, porque protegem terceiros e
não o usuário:

1. **Nunca convidados.** Convidado dispara e-mail para gente real, e um convite
   errado não se desfaz apagando o evento.
2. **Só na agenda dedicada "Terminal Assistant"**, uma por conta. É o que mantém
   todo o estrago apagável de uma vez, e é por isso que `_criar_evento_automatico`
   **não escreve em lugar nenhum** quando essa agenda não existe, em vez de cair
   para a agenda principal.

Duas contenções novas, que não existiam no fluxo manual:

- **Piso de confiança de 0,7.** Abaixo dele a revisão não faz nada. O modelo é
  instruído a devolver confiança baixa na dúvida, e uma correção errada é pior
  que nenhuma.
- **Todo ato autônomo notifica.** Se o app mexeu num prazo ou escreveu na agenda,
  aparece na tela na hora. Autonomia invisível é pior que autonomia nenhuma:
  sem o aviso, o usuário perde a chance de desfazer enquanto ainda lembra do
  contexto.

`TA_AUTO_REVIEW=0` desliga a segunda passada inteira, e a captura volta a ser só
o regex. A rota `/calendar/event` **continua exigindo `confirmed: true`**: o
caminho manual não foi afrouxado, o que mudou é que agora existe um caminho
automático ao lado dele.

