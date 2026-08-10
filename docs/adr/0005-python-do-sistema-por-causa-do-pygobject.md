# O projeto roda no Python do sistema, não no do brew

Falar com o Evolution Data Server exige PyGObject, e nesta máquina `gi` existe
apenas em `/usr/bin/python3` (3.12), instalado via `python3-gi` em
`/usr/lib/python3/dist-packages`. O `python3` que responde no PATH é o do
Homebrew (3.14) e **não** tem `gi`. O projeto portanto fixa o interpretador do
sistema e cria seu virtualenv com `--system-site-packages`, para que o venv
enxergue os bindings do sistema.

Isso não é visível em nenhum arquivo de código, e um `python3 -m venv` feito por
reflexo produz um ambiente onde `import gi` falha sem explicação óbvia.

## Considered Options

- **Manter a linguagem principal em outra stack** (TypeScript ou Go) e isolar a
  agenda num pequeno processo Python. Rejeitado: coloca a integração mais frágil
  do projeto atrás de uma fronteira de processo, que é onde bug vira mistério.
- **Instalar PyGObject via pip no Python do brew.** Rejeitado: exige toolchain de
  compilação e, mesmo compilando, ainda depende dos typelibs do sistema.

## Consequences

- O projeto fica preso à versão de Python que o Ubuntu fornece. Em troca, ganha
  estabilidade: essa versão só muda quando o sistema muda.
- Também são necessários os typelibs `gir1.2-ecal-2.0` e
  `gir1.2-edataserver-1.2`, que não vêm instalados por padrão.
