# Loopback por padrão, e token para sair dele

O daemon escutava em `0.0.0.0` desde o primeiro dia, com um `# noqa: S104` no
código dizendo "LAN de propósito: o mural abre no celular". Era escolha
consciente, e para uma máquina só, numa rede doméstica, é defensável.

Ele expõe 29 rotas e **não tem autenticação nenhuma**. Qualquer um na mesma rede,
sem credencial, pode:

- ler todas as anotações pessoais (`GET /notes`);
- apagá-las (`/rm`, `/purge`);
- acender e apagar as luzes da casa, usando o token de Home Assistant do dono;
- descobrir a URL do HA e quais segredos estão configurados (`/health`);
- gastar a chave do modelo, uma chamada por requisição.

Publicar isso com um passo a passo que manda estranhos rodarem
`make install-service` transfere esse risco para quem não pediu, e em redes que
não são a sala de casa: café, coworking, universidade, wifi de prédio.

Então o padrão passa a ser `127.0.0.1`, e abrir para a rede exige **dois** atos
deliberados: `TA_HOST` e `TA_TOKEN`. Sem token, o daemon **recusa subir** em
endereço não-loopback, com o comando do conserto na mensagem.

O middleware só existe quando há token, e **quem chega do loopback passa sem
credencial**. Não é frouxidão: quem já está na máquina tem o `.env`, e exigir
token dele faria `ta note` carregar segredo sem comprar segurança nenhuma. O que
o middleware cobre é a rede.

## Considered Options

- **Manter `0.0.0.0` e documentar no SECURITY.md.** Rejeitado pelo desequilíbrio
  de quem lê o quê: quem abre o documento de segurança já se preocupa; quem segue
  o passo a passo é justamente quem não sabe que devia.
- **Loopback por padrão, sem token nenhum.** Muito mais barato — nenhuma mudança
  no mural. Rejeitado porque o celular é metade da graça do mural, então quase
  todo mundo vai abrir, e aí volta ao estado anterior com um passo a mais.
- **Token obrigatório sempre, inclusive em loopback.** Mais uniforme, sem caso
  especial no código. Rejeitado por atrapalhar o uso local legítimo: `ta note`
  precisaria carregar credencial, e "instale e use" deixaria de ser imediato.
- **Autenticação de verdade — usuário, senha, sessão.** Rejeitado por
  desproporção: é uma ferramenta de uma máquina, e um segredo compartilhado
  resolve o modelo de ameaça inteiro (a rede local) sem trazer gestão de conta.
- **HTTPS.** Não resolve nada aqui e custa caro: certificado para IP de LAN é
  autoassinado, o navegador do celular reclama, e o atacante do modelo de ameaça
  está na mesma rede — ele precisa da credencial, não de interceptar tráfego. Se
  um dia o daemon sair da LAN, isso muda, e aí a resposta é um túnel, não TLS
  caseiro.

## Consequences

- **O mural no celular passou a exigir um passo.** O endereço vira
  `http://<ip>:7777/board?token=…`. O token entra uma vez, vai para o
  `localStorage` e **sai da barra de endereço no mesmo instante** — senão fica no
  histórico do navegador e em todo link colado.
- **A instalação atual do autor quebra**, e é o único ponto deste trabalho com
  decisão humana: o serviço rodava aberto. O `ta doctor` detecta, gera o token e
  imprime as duas linhas para o `.env`.
- **`create_app` passou a poder falhar.** A checagem mora ali, e não no `main()`,
  para valer também para quem monta o app por conta própria. Isso torna a falha
  visível em teste, que é onde ela deve aparecer.
- **Comparação de token em tempo constante** (`hmac.compare_digest`). Comparar
  segredo com `==` vaza o prefixo correto pelo tempo de resposta. É barato fazer
  certo e caro descobrir depois.
- **`::ffff:127.0.0.1` conta como local.** Um socket IPv6 aceitando IPv4 reporta o
  par nessa forma, e sem tratá-la o CLI local passaria a precisar de token
  dependendo da família do socket — falha que só apareceria na máquina de outra
  pessoa.
- **O `/health` continua reportando presença de segredo, nunca o valor.** A
  disciplina é antiga e agora tem teste.
