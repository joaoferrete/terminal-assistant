# A hora não decide se a regra roda; decide o que ela faz

O pedido original era "depois das 16h, quando eu entrar numa call, acende a luz e
o ringlight". A regra nasceu assim, com a hora no `when=` do gatilho de entrada —
antes das 16h, nada acontecia.

Isso estava errado nos dois sentidos. Uma call às 10h não acendia nada, embora a
luz no máximo ajude numa chamada a qualquer hora. E a luz caía para o nível de
estar ao sair de uma reunião das 18h, devolvendo o quarto ao escuro justamente
quando o sol já tinha ido embora.

O erro foi tratar `16:00` como um interruptor da regra. Ele nunca foi isso: o que
`16:00` quer dizer é **"já escureceu"**. E escurecer não muda *se* você está numa
reunião — muda o que é útil fazer a respeito.

Então a hora saiu do `when=` e virou condição interna, governando três coisas:

| Efeito | Antes das 16h | Depois |
|---|---|---|
| Luz no máximo, ao entrar | sim | sim |
| Ringlight | não | sim |
| Luz volta ao nível de estar, ao sair | sim | não |

A luz é incondicional porque ajuda numa call de qualquer horário. O ringlight só
entra depois que escurece: de dia a luz natural já dá conta, e a borda acesa é
produção que ninguém pediu. E a luz só volta ao nível de estar enquanto ainda for
cedo, porque depois disso baixá-la é devolver o quarto ao escuro.

## Considered Options

- **Manter `when=after("16:00")` e aceitar o buraco.** É o que existia. Rejeitado
  porque o caso frequente — call de manhã — não é exceção, é a maioria dos dias.
- **Duas regras de entrada, uma para cada lado das 16h.** Ficaria explícito no
  `ta rules check`. Rejeitado porque as duas compartilhariam a checagem de 1:1, o
  título da agenda e a luz — a diferença real é uma linha, e duas regras quase
  idênticas divergem na primeira manutenção.
- **Guardar o nível anterior da luz na entrada e restaurá-lo na saída.** Mais
  correto no papel: devolveria exatamente o que estava antes, em vez de um `40`
  fixo. Rejeitado porque introduz estado entre dois disparos que podem estar
  separados por horas, por um restart do daemon, ou por alguém ter mexido na luz
  pelo celular no meio. O estado envelheceria calado, e a luz voltaria para um
  valor que ninguém reconhece.
- **Ler o pôr do sol do `sun.sun` do Home Assistant em vez de fixar `16:00`.** É o
  sinal verdadeiro. Rejeitado por ora porque amarraria ao HA uma decisão que hoje
  é tomada sem ele — a regra funciona com o HA fora do ar, só sem a parte da luz —
  e porque uma constante legível no topo do arquivo é mais fácil de ajustar que
  uma dependência a mais. Fica como melhoria óbvia.
- **Desligar o ringlight na saída só se ele teria sido ligado**, repetindo a
  condição da entrada. Rejeitado por um caso concreto: a reunião que começa às
  15h50 e termina às 16h10 não ligou a borda (era cedo) mas *desligaria* pela
  condição da saída — ou, invertendo a condição, ligaria e nunca desligaria.
  `enable(False)` incondicional não tem esse buraco, e desligar o que já está
  desligado não custa nada.

## Consequences

- **A hora é lida no instante do disparo**, não no início da chamada. Uma reunião
  que atravessa as 16h conta como escura na saída. É o que se quer: quem decide é
  a janela em que a ação acontece, não a agenda.
- **Um helper só decide o que `16:00` significa** (`_ja_escureceu`), lido pelos
  dois usos. É a mesma primitiva `after()` que uma `when=` usaria, então os três
  lugares não podem divergir.
- **O texto da notificação passou a depender da hora.** Anunciar "luz e ringlight"
  de manhã seria mentira barata, e é assim que se deixa de confiar no aviso. Tem
  teste, porque é o tipo de detalhe que ninguém revisa.
- **A checagem de 1:1 passou a existir nos dois lados.** Antes só a entrada
  consultava a agenda; se nada foi aceso, nada deve ser desfeito. Isso depende de
  o compromisso ainda estar em curso — `calendar.now()` só devolve evento entre
  `start` e `end` —, então quem sai **depois** do horário marcado cai no caminho
  comum e tem a luz ajustada. Errar para o lado de ajustar é o lado barato.
- **O nome da regra mudou de `reuniao_tarde` para `reuniao`**, porque ela não é
  mais sobre a tarde. A documentação citava o nome antigo em dois arquivos e
  ninguém notou — agora um teste compara a doc com as regras que carregam de
  verdade.
- **`16:00` continua sendo um chute.** É a hora em que costuma escurecer no
  inverno de quem escreveu, e não vale para o verão nem para outra latitude. Está
  numa constante nomeada no topo do arquivo por isso.
