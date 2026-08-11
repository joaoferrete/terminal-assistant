# A configuração e as regras do usuário saem do repositório

Os apelidos de `entity_id` moravam em `src/ta/config.py`, como código-fonte:

```python
ENTITY_ALIASES = {
    "quarto": "light.lampada_do_quarto",
    "ventilador": "switch.ventilador_socket_1",
}
```

E as Rules moravam em `<repo>/rules/`, resolvido a partir do próprio arquivo do
pacote. O `rules/reuniao.py` abre dizendo, textualmente, "este arquivo é
versionado de propósito".

Enquanto o repositório foi de uma pessoa, as duas coisas estavam certas. É a casa
dela, versionada junto do código que a comanda, e editar e commitar é o fluxo.

Aberto, as mesmas duas frases viram defeitos. Para configurar a própria casa, quem
clonar precisa **editar arquivos dentro do pacote instalado** — mudança que se
perde em qualquer reinstalação — e **dentro do checkout do git**, o que conflita
em todo `git pull`. E `rules/reuniao.py` carregaria no boot dessa pessoa mirando
uma lâmpada que não existe na casa dela.

Então:

- Apelidos e grupos vão para `~/.config/ta/config.toml`, respeitando
  `XDG_CONFIG_HOME`.
- As Rules do usuário vão para `~/.config/ta/rules/`.
- O `rules/` do repositório vira `examples/rules/`: documentação versionada, **não
  carregada**.

O padrão não é novo no projeto — `db.default_db_path()` já punha o banco em
`~/.local/share/ta/`. A configuração só não tinha acompanhado.

## Considered Options

- **Variáveis de ambiente no `.env` que já existe.** `TA_ALIASES="quarto=light.x"`
  não custaria arquivo novo e reusaria o `EnvironmentFile` do systemd. Rejeitado
  porque não cobre as Rules — código Python não cabe em variável de ambiente — e
  porque string com sintaxe própria envelhece mal assim que alguém quiser um
  grupo.
- **Deixar como está, documentando "forke e edite".** Coerente com "projeto
  pessoal aberto para leitura", e foi rejeitado junto com aquele posicionamento:
  garante conflito de merge em toda atualização.
- **Só os apelidos saírem, as Rules ficarem no repositório.** Menos trabalho, e
  deixa exatamente o pior pedaço de pé — a regra de exemplo carregando na casa
  errada.
- **Um diretório de config dentro do repositório** (`.ta/`, no `.gitignore`).
  Mantém tudo junto e é fácil de achar. Rejeitado porque amarra a configuração ao
  checkout: mover ou reclonar o repositório perderia a casa, e um segundo checkout
  teria uma segunda casa.
- **Carregar `examples/rules/` também, por conveniência.** Rejeitado: uma Rule que
  mira uma entity inexistente não dá erro útil — o Home Assistant responde e nada
  acontece. Exemplo tem que ser copiado conscientemente.

## Consequences

- **Nada quebra na instalação existente.** O carregador prefere
  `~/.config/ta/rules/` e **cai de volta** para `<repo>/rules` quando o destino
  novo não existe, logando por quê. `ta doctor` **copia**, nunca move.
- **Uma instalação sem `config.toml` é um estado utilizável, não quebrado.** É o
  estado de todo mundo que clonar. `ta on light.o_que_for` continua exato, e
  `ta on quarto` ainda casa por trecho do nome vindo do inventário do próprio
  Home Assistant — a resolução por ambiente nunca dependeu de apelido.
- **Grupos continuam embutidos; apelidos, não.** `luz`, `tomada` e `tudo` valem
  para qualquer casa e não são pessoais. O arquivo do usuário acrescenta, e um
  nome repetido substitui.
- **`config.toml` quebrado não derruba o daemon** — vira `WARNING` e apelidos
  vazios. Falhar por causa de um arquivo de conveniência seria desproporcional;
  falhar **calado** seria pior, porque o sintoma é `ta on quarto` parando de
  funcionar sem explicação.
- **Um teste garante que nenhum `entity_id` concreto volta para `config.py`.** O
  modo de falha que isso causa só aparece na máquina de outra pessoa, que é tarde
  demais. Ele pegou até um `entity_id` deixado num comentário.
- **`RULES_DIR` virou `user_rules_dir()`**, e não `rules_dir()`, porque
  `create_app` tem um parâmetro com esse nome e a sombra faria a chamada
  silenciosamente virar outra coisa.
