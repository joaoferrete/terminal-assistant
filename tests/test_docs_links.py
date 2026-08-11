"""A documentação virou dez arquivos que se apontam. Link quebrado é silencioso.

A regra de dono único — cada fato mora em exatamente um arquivo, e todo o resto
linka — só funciona enquanto os links funcionam. Um `docs/aliases.md` renomeado
para `shortcuts.md` deixa seis páginas apontando para o vazio, e nada avisa: o
GitHub renderiza o link, ele só dá 404 em quem clica.

Também guarda a outra ponta: um comando novo que ninguém documentou some da única
listagem que existe, porque a lista plana do argparse foi removida de propósito.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Todo markdown do projeto, menos o que é local e não versionado.
DOCS = sorted(
    p for p in REPO.rglob("*.md")
    if ".venv" not in p.parts and p.name != "ROADMAP.md"
)

# `[texto](destino)`, ignorando imagens e referências de link.
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")


def test_ha_documentacao():
    """Guarda contra o teste passar porque não achou arquivo nenhum."""
    assert len(DOCS) >= 15, [p.name for p in DOCS]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_nenhum_link_relativo_quebrado(doc: Path):
    quebrados = []
    for destino in LINK.findall(doc.read_text()):
        if destino.startswith(("http://", "https://", "mailto:")):
            continue
        # Âncora dentro do próprio arquivo: o alvo é a página, não o heading —
        # verificar heading exigiria reimplementar a geração de slug do GitHub,
        # que erraria mais do que acertaria.
        caminho = destino.split("#")[0]
        if not caminho:
            continue
        if not (doc.parent / caminho).exists():
            quebrados.append(destino)

    assert not quebrados, f"{doc.relative_to(REPO)} aponta para o vazio: {quebrados}"


def test_as_imagens_do_readme_existem():
    """Imagem quebrada no README é a primeira coisa que um visitante vê."""
    corpo = (REPO / "README.md").read_text()
    for destino in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", corpo):
        if destino.startswith("http"):
            continue
        assert (REPO / destino).exists(), f"imagem ausente: {destino}"


def test_todo_comando_aparece_na_documentacao():
    """Um comando que não está em doc nenhuma não existe para quem lê.

    A lista plana do `argparse` foi removida em favor do epílogo agrupado, então
    a documentação é a única listagem completa que existe.
    """
    from ta.cli import build_parser

    comandos = set(build_parser()._subparsers._group_actions[0].choices)
    texto = "\n".join(p.read_text() for p in DOCS)

    ausentes = {c for c in comandos if f"ta {c}" not in texto}
    assert not ausentes, f"comando sem menção na documentação: {sorted(ausentes)}"


def test_os_adr_citados_existem():
    """ADR renumerado ou renomeado quebra a explicação, não só o link."""
    adr = {p.name for p in (REPO / "docs" / "adr").glob("*.md")}
    texto = "\n".join(p.read_text() for p in DOCS)

    citados = set(re.findall(r"(\d{4}-[a-z0-9-]+\.md)", texto))
    assert citados - adr == set(), f"ADR citado e inexistente: {sorted(citados - adr)}"
