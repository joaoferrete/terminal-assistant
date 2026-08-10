# Agenda pelo GNOME Online Accounts, não por OAuth próprio

O app precisa ler e escrever em duas agendas Google (uma pessoal e uma de
trabalho). As contas são conectadas em Configurações → Contas Online, e o app
conversa por DBus com o Evolution Data Server, que o GNOME já mantém sincronizado.
O escopo de Calendar do Google é sensível, e um OAuth próprio em publishing status
"Testing" emite refresh token que expira a cada sete dias — reautenticar as duas
contas para sempre é inviável para um daemon. O client OAuth do GNOME já é
verificado e o GOA renova o token sozinho.

## Considered Options

- **OAuth próprio contra a Google Calendar API.** REST limpa e bem documentada,
  mas carrega a expiração de sete dias descrita acima; escapar dela exige submeter
  o app à verificação do Google. Há ainda o risco de o admin do Workspace da conta
  de trabalho bloquear um app de terceiro não verificado.
- **Somente leitura, por feed ICS**, deixando a criação de evento para um link que
  o usuário clica. Rejeitado: abre mão de um requisito explícito.

## Consequences

- A API do EDS é mais trabalhosa que REST, e é o motivo pelo qual a linguagem do
  projeto ficou amarrada (ver ADR 0005).
- A leitura da agenda passa a ser local e instantânea, porque o EDS sincroniza em
  background. Isso é o que viabiliza usar a agenda como condição de uma Rule sem
  pagar latência de rede.
- O app depende de o usuário ter conectado as contas no GNOME. Sem isso não há
  agenda, e a falha precisa ser dita com clareza em vez de parecer um bug.
