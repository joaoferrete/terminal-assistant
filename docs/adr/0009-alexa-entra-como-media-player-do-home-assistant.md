# A Alexa entra como `media_player` do Home Assistant

Comandar os Echo — falar um anúncio, tocar música, ajustar volume — passa pela
integração de comunidade `alexa_media_player` instalada **no Home Assistant**,
que expõe cada Echo como uma entidade `media_player.*`. O app não ganha nenhum
código específico de Alexa: ele continua endereçando `entity_id` e chamando
serviços do HA, exatamente como faz com a lâmpada.

O ganho é de contenção. `alexa_media_player` autentica com cookie de sessão
reverso-engenheirado e é a peça menos confiável de todo o projeto — quebra quando
a Amazon muda algo do lado dela. Mantendo-a atrás do Home Assistant, essa
fragilidade fica num lugar só, e o dia em que ela quebrar não é o dia em que este
repositório precisa mudar.

## Considered Options

- **Dispositivo virtual + Rotina da Alexa** — o HA expõe um interruptor falso e
  uma Rotina dispara com ele. Usa apenas API oficial nas duas pontas e é bem mais
  confiável. Rejeitado por não atender ao requisito: Rotina tem texto e ação
  fixos, então não há como pedir volume dinâmico nem "toca tal música", e cada
  Rotina exigiria um interruptor falso próprio. Continua sendo a opção certa se
  algum dia o requisito virar apenas disparar cenas pré-definidas.
- **Voice Monkey**, serviço de terceiro com webhook. Rejeitado: acrescenta uma
  dependência de nuvem sem resolver a parte de mídia.
- **Falar com a Amazon direto do app.** Rejeitado pelo mesmo motivo registrado no
  ADR 0001: a API oficial da Alexa é destinada ao fabricante do aparelho, não ao
  usuário final.

## Consequences

- **A notificação de desktop nunca é substituída, só complementada.** Um Reminder
  que só falasse no Echo silenciaria junto com a integração no dia em que ela
  quebrasse. O anúncio é adicional; o caminho confiável continua sendo o
  obrigatório.
- Essa integração vai precisar de manutenção periódica, diferente do resto do
  projeto. Quando `ta play` parar de funcionar, o primeiro lugar a olhar é a
  autenticação da Amazon no HA, não o código daqui.
