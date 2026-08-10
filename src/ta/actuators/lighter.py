"""Atuador da extensão Lighter, por gsettings.

A Lighter (https://github.com/joaoferrete/Lighter) é do próprio usuário. O daemon
manda no *quê* — qual profile aplicar — e a extensão manda no *como*, porque a
calibração da borda está tunada para a posição da webcam e não deve ser
duplicada aqui (ADR 0002, decisão 12).

Verificado no código dela: `extension.js` conecta um handler genérico de `changed`
sobre as chaves de aparência, e o GSettings notifica escrita externa — então
`gsettings set` de fora funciona ao vivo, sem alterar a extensão. A única
alteração necessária foi um listener de `changed::active-profile`, para que
aplicar um profile *por nome* fosse possível de fora.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("ta.lighter")

SCHEMA = "org.gnome.shell.extensions.lighter"
UUID = "lighter@gnome-shell-extensions.ferrete.com"

# O schema de uma extensão do GNOME NÃO fica no caminho de busca padrão do
# gsettings — ele vive dentro do diretório da própria extensão. Sem `--schemadir`
# todo comando falha com "Nenhum esquema org.gnome.shell.extensions.lighter".
# Descoberto na prática; o caminho é resolvido, nunca fixado.
SCHEMA_DIRS = (
    Path.home() / ".local/share/gnome-shell/extensions" / UUID / "schemas",
    Path("/usr/share/gnome-shell/extensions") / UUID / "schemas",
)


def _schemadir() -> Path | None:
    for d in SCHEMA_DIRS:
        if (d / "gschemas.compiled").is_file():
            return d
    return None


class Lighter:
    def __init__(self) -> None:
        self._bin = shutil.which("gsettings")
        self._dir = _schemadir()
        if self._bin is None:
            log.warning("gsettings não encontrado: a Lighter fica fora de alcance")
        elif self._dir is None:
            log.warning("schema da Lighter não encontrado; a extensão está instalada?")

    @property
    def available(self) -> bool:
        return self._bin is not None and self._dir is not None

    async def _run(self, *args: str) -> str | None:
        if not self.available:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, "--schemadir", str(self._dir), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                log.error("gsettings %s falhou: %s", " ".join(args), err.decode().strip())
                return None
            return out.decode().strip()
        except Exception:
            log.exception("gsettings falhou")
            return None

    async def get(self, key: str) -> str | None:
        raw = await self._run("get", SCHEMA, key)
        # gsettings devolve string com aspas simples em volta; o chamador quer o valor.
        if raw and len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
            return raw[1:-1]
        return raw

    async def set(self, key: str, value: str) -> bool:
        return await self._run("set", SCHEMA, key, value) is not None

    # ── Estado ──────────────────────────────────────────────────────────────
    async def enable(self, on: bool = True) -> bool:
        return await self.set("enabled", "true" if on else "false")

    async def toggle(self) -> bool:
        atual = await self.get("enabled")
        return await self.enable(atual != "true")

    async def profiles(self) -> list[dict]:
        """Os profiles salvos na extensão. O daemon não os mantém — ela mantém."""
        raw = await self.get("profiles")   # `get` já removeu as aspas
        if not raw:
            return []
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            log.error("JSON de profiles da Lighter ilegível")
            return []
        return doc.get("profiles", doc) if isinstance(doc, dict) else doc

    async def profile_id(self, nome: str) -> str | None:
        """Resolve profile por *nome*, não por uid.

        Fixar o uid no código seria o mesmo erro que evitamos com as agendas: ele
        muda se o profile for recriado.
        """
        for p in await self.profiles():
            if p.get("name", "").lower() == nome.lower():
                return p.get("id")
        return None

    async def apply_profile(self, nome: str, *, enable: bool = True) -> bool:
        """Aplica um profile por nome e acende a borda.

        Depende do listener de `changed::active-profile` adicionado na extensão.
        Sem ele, escrever a chave não aplica nada — e o silêncio é a pior parte:
        não há erro, a borda só não muda.
        """
        pid = await self.profile_id(nome)
        if pid is None:
            log.error("profile %r não existe na Lighter", nome)
            return False
        ok = await self.set("active-profile", pid)
        if ok and enable:
            ok = await self.enable(True)
        return ok

    # ── Guarda contra corrida ───────────────────────────────────────────────
    async def take_over(self) -> None:
        """Desliga o `auto-switch` da extensão enquanto o daemon está no comando.

        Sem isso, o `WindowWatcher` dela aplicaria um profile no próximo
        `notify::focus-window` e sobrescreveria o que a Rule acabou de fazer.
        Hoje `auto-switch` já vem `false`, então isto é uma garantia, não um
        conserto.
        """
        if (await self.get("auto-switch")) == "true":
            log.info("desligando auto-switch da Lighter enquanto o daemon estiver de pé")
            await self.set("auto-switch", "false")

    async def hand_back(self) -> None:
        """Devolve a autonomia à extensão quando o daemon sai.

        Chamado no desligamento limpo. Se o daemon morrer de morte matada, a
        chave fica `false` — e a extensão continua controlável pela UI dela.
        """
        await self.set("auto-switch", "true")
