# Home Assistant como camada de device

O hardware atual é WiFi Tuya (lâmpadas Positivo, tomada Ekasa), mas a intenção
declarada é migrar para Zigbee no futuro sem data definida. Colocamos o Home
Assistant como única camada entre o app e os aparelhos: o app fala REST/WebSocket
com o HA e nunca conhece o protocolo do aparelho. Assim, trocar de Tuya para
Zigbee (ou Matter) passa a ser um pareamento no HA, não uma alteração de código.

## Considered Options

- **`tinytuya` direto, com interface de adapter.** Rejeitado: não elimina a
  necessidade de infraestrutura no futuro. Zigbee exigiria `zigbee2mqtt` ou ZHA
  de qualquer forma, chegando no mesmo destino por um caminho mais longo, e com
  um adapter escrito à mão para manter.
- **`tinytuya` direto, sem abstração.** Rejeitado: assume conscientemente a
  reescrita do módulo de casa quando o hardware mudar.

Vale registrar por que a nuvem do Google não aparece como opção: as Home APIs
novas são SDK apenas para Android e iOS; a Smart Device Management cobre somente
aparelhos Nest; e o Assistant SDK está descontinuado. Não existe caminho oficial
do Google para controlar uma lâmpada de terceiro a partir de um CLI Linux.
A Alexa é pior — sua API de smart home é destinada ao fabricante do aparelho,
não ao usuário final.

## Consequences

- O app depende de um serviço externo estar de pé. Um HA parado significa módulo
  de casa indisponível, e o app precisa degradar com clareza em vez de travar.
- O HA passa a ser dono do inventário e da nomenclatura dos aparelhos. O app não
  mantém sua própria lista.
- Ganhamos de graça um dashboard web, app de celular e histórico de estado.
