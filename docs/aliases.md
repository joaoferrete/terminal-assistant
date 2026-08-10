# Aliases e atalhos de teclado

Duas superfícies para chamar o `ta` sem digitar o nome inteiro: aliases no zsh,
para quando você já está no terminal, e atalhos globais do GNOME, para quando não
está.

## Aliases (zsh)

Cole no fim do `~/.zshrc`, depois `source ~/.zshrc`. Nenhum destes colide com os
aliases que você já tem.

```zsh
# --- terminal-assistant ---
# Casa
alias acende="ta on"               # acende luz | acende quarto | acende quarto 40
alias apaga="ta off"               # apaga | apaga luz | apaga ventilador
alias luz="ta luz"                 # o nome antigo, mantido pela familiaridade
alias ringlight="ta lighter toggle"
alias casa="ta temp"               # clima, e se o ventilador está puxando energia

# Notas
alias nota="ta note"               # nota "ligar dentista @sexta #saude !alta"
alias notas="ta list"              # as abertas
alias quadro="ta board"            # abre o mural no navegador
alias organiza="ta organize"       # roda o LLM e grava a ordem

# Dia
alias hoje="ta today"              # compromissos + tarefas, por prioridade

# Manutenção
alias revisa="ta revise"           # LLM reetiqueta tudo que está aberto
alias lixo="ta rm --list"          # o que foi apagado
```

Se o registro em português incomodar depois, renomear é uma linha cada.

**`acende` sem número acende no máximo**, e o termo pode ser um grupo (`luz`,
`luzes`, `tomada`, `tudo`), um ambiente (`quarto`) ou um `entity_id` inteiro. A
tabela completa de resolução está no [README](../README.md#grupos-e-ambientes).

### Antes dos aliases: o `ta` precisa estar no PATH

Este é o passo que costuma faltar, e o sintoma é confuso — os comandos de casa
"não funcionam" quando na verdade o executável não é encontrado. Aconteceu neste
projeto:

```zsh
export PATH="$HOME/terminal-assistant/.venv/bin:$PATH"
```

Cole **antes** dos aliases e confira com `which ta`. Se não imprimir nada, os
aliases não vão funcionar e nenhuma mensagem de erro vai explicar por quê.

### Sintaxe da captura

A captura é determinística e não precisa de rede. O parser reconhece:

| Marca | Significado | Efeito na Note |
|---|---|---|
| `#tag` | tema | agrupa |
| `!alta` `!media` `!baixa` | prioridade | ordena, **colore o post-it** e aparece no `hoje` |
| — | tipo e área | a revisão etiqueta sozinha: `#tarefa`/`#compromisso`/`#anotacao` e `#trabalho`/`#pessoal` |
| `@sexta` `@2026-08-14` `@amanha` | prazo | passa a ser Task (cobrável) |
| `@@22:00` | instante de disparo | passa a ser Reminder (notifica) |

A cor do post-it segue esta precedência: **sua escolha** → **prioridade cruzada
com a área** (a prioridade escolhe a família, a área desloca o tom: trabalho para
o frio, pessoal para o quente) → **área sozinha**, quando não há prioridade →
papel neutro. O botão `A` de cada
post-it devolve a cor automática, e a tabela completa com os hex está no
[README](../README.md#cor-do-post-it).

O `×` do post-it apaga de forma reversível; a lixeira é `/board#lixeira`, ou
`ta rm --list` no terminal.

Texto sem nenhuma marca continua sendo uma Note válida, e vai para a coluna de
entrada. Nada é obrigatório.

## Atalhos globais (GNOME / Wayland)

No Wayland, atalho global é registrado no GNOME e executa um comando. Três
armadilhas, todas reais:

1. **A lista `custom-keybindings` é reescrita por inteiro, nunca incrementada.**
   Se você setar só os atalhos novos, os antigos desaparecem. Os comandos abaixo
   já preservam os seus dois existentes: `vscode` (`<Alt>c`) e `Intellij`
   (`<Alt>i`).
2. **Alias de shell não funciona aqui.** O GNOME não abre um shell interativo, então
   `luz` não existe para ele. Use o caminho absoluto do executável.
3. **`<Control><Alt>l` já é da [Lighter](https://github.com/joaoferrete/Lighter)** (`open-prefs-shortcut`). Por isso os
   atalhos abaixo usam `<Super><Alt>`, onde só as setas estão ocupadas.

### Registrar

```zsh
SD=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
TA="$HOME/terminal-assistant/.venv/bin/ta"   # caminho absoluto, não alias

# 1. A lista completa: os dois que você já tinha + os quatro novos.
gsettings set $SD custom-keybindings "[
  '$P/custom0/', '$P/custom1/',
  '$P/custom2/', '$P/custom3/', '$P/custom4/', '$P/custom5/'
]"

# 2. Três chaves por atalho: name, command, binding.
set_kb() {  # set_kb <customN> <nome> <comando> <atalho>
  local k="$SD.custom-keybinding:$P/$1/"
  gsettings set "$k" name    "$2"
  gsettings set "$k" command "$3"
  gsettings set "$k" binding "$4"
}

set_kb custom2 'TA: acender luz' "$TA on luz"        '<Super><Alt>l'
set_kb custom3 'TA: apagar'     "$TA off"            '<Super><Alt>o'
set_kb custom4 'TA: nota'       "$TA capture-popup"  '<Super><Alt>n'
set_kb custom5 'TA: ringlight'  "$TA lighter toggle" '<Super><Alt>r'
```

### Conferir

```zsh
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```

Deve listar seis caminhos. Se listar quatro, a lista foi sobrescrita e os atalhos
do vscode e do Intellij precisam ser recolocados.

Para inspecionar um atalho específico:

```zsh
SD=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
for k in name command binding; do
  gsettings get "$SD.custom-keybinding:$P/custom2/" $k
done
```

### Nota sobre a interface gráfica

Configurações → Teclado gerencia esses mesmos atalhos e usa a convenção
`custom0`, `custom1`, … — que é a que os comandos acima seguem, justamente para
que os dois caminhos convivam. Se você adicionar um atalho pela interface depois,
ele entra como `custom6` e a lista é atualizada sozinha.

## Como saber se o daemon está de pé

Todos os aliases falam HTTP com o daemon. Se ele não estiver rodando, o comando
deve falhar com mensagem legível em vez de travar:

```zsh
systemctl --user status ta
systemctl --user restart ta
journalctl --user -u ta -f      # acompanhar as automações disparando
```
