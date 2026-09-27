"""Files on a Satellite (F9, D41): list, read and send, from the laptop's own folders.

What must hold: nothing outside `[files] folders` leaves the laptop, whatever
path the server names; secrets and hidden files never do; only the asker's own
computer answers the asker; and file contents taint the turn.
"""
import asyncio
import base64
import json

import httpx
import pytest
from test_satellite import TOKEN, bearer, server  # noqa: F401 - the fixture

from ta import builtin_tools, db, satellite_files
from ta.grants import OWNER_PERMISSIONS
from ta.satellite import Hub
from ta.tools import Turn, registered
from ta.tools import run as run_tool


# ── The laptop decides ──────────────────────────────────────────────────────
@pytest.fixture
def home(tmp_path):
    shared = tmp_path / "Documentos"
    (shared / "contas").mkdir(parents=True)
    (shared / "contas" / "luz.txt").write_text("conta de luz: R$ 120")
    (shared / "manual.pdf").write_bytes(b"%PDF-1.4 \x00\xff binary")
    (shared / ".ssh").mkdir()
    (shared / ".ssh" / "config").write_text("Host casa")
    (shared / "api_token.txt").write_text("sk-nope")
    (shared / "node_modules").mkdir()
    outside = tmp_path / "privado"
    outside.mkdir()
    (outside / "diario.txt").write_text("não é para sair")
    (shared / "atalho").symlink_to(outside / "diario.txt")
    return shared, [str(shared)]


def ask(op, path, folders):
    return satellite_files.handle({"op": op, "path": path}, folders)


def test_a_file_in_a_shared_folder_can_be_read(home):
    shared, folders = home
    out = ask("read", str(shared / "contas" / "luz.txt"), folders)
    assert out["text"] == "conta de luz: R$ 120"
    assert ask("read", "contas/luz.txt", folders)["text"] == out["text"], "relative works"


@pytest.mark.parametrize("path", [
    "../privado/diario.txt",            # climbing out
    "atalho",                           # a symlink pointing out
    "/etc/passwd",
    ".ssh/config",                      # hidden, inside an allowed folder
    "api_token.txt",                    # looks like a secret
    "node_modules",
])
def test_nothing_outside_or_secret_leaves_the_laptop(home, path):
    _, folders = home
    for op in ("read", "send", "list"):
        assert "error" in ask(op, path, folders), (op, path)


def test_with_no_shared_folder_nothing_is_shared(home):
    assert "no folder is shared" in ask("read", "contas/luz.txt", [])["error"]


def test_a_listing_hides_what_cannot_be_opened(home):
    shared, folders = home
    names = [e["path"].rsplit("/", 1)[-1] for e in ask("list", str(shared), folders)["entries"]]
    assert names == ["contas", "manual.pdf"], "no .ssh, no token, no node_modules, no symlink"
    assert ask("list", "", folders)["entries"][0]["dir"] is True


def test_a_binary_is_sent_not_read(home):
    _, folders = home
    assert "not a text file" in ask("read", "manual.pdf", folders)["error"]
    sent = ask("send", "manual.pdf", folders)
    assert sent["name"] == "manual.pdf"
    assert base64.b64decode(sent["data"]).startswith(b"%PDF")


def test_a_long_text_is_truncated(home, monkeypatch):
    shared, folders = home
    monkeypatch.setattr(satellite_files, "MAX_READ_CHARS", 5)
    out = ask("read", "contas/luz.txt", folders)
    assert out["text"] == "conta" and out["truncated"]


def test_nothing_writes_and_nothing_raises(home):
    _, folders = home
    assert "unknown operation" in ask("delete", "contas/luz.txt", folders)["error"]
    assert "does not exist" in ask("read", "nada.txt", folders)["error"]


def test_the_satellite_answers_the_server_with_what_its_config_allows(home, monkeypatch):
    from ta.config import Config
    from ta.satellite_client import Satellite

    _, folders = home
    monkeypatch.setattr("ta.config.files_folders", lambda: folders)
    posted = []

    def server_(request):
        if request.url.path == "/satellite/actions":
            return httpx.Response(200, json={"actions": [
                {"kind": "files", "id": "q1", "op": "read", "path": "contas/luz.txt"}]})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    sat = Satellite(Config(server="http://srv", token=TOKEN),
                    lighter=object(), notifier=object(), transport=httpx.MockTransport(server_))
    asyncio.run(sat.poll_once())
    assert posted[0]["id"] == "q1" and "R$ 120" in posted[0]["answer"]["text"]


# ── The hub: a question and its answer ──────────────────────────────────────
def test_only_the_asked_satellite_can_answer():
    async def go():
        hub = Hub()
        pending = asyncio.create_task(hub.ask(1, {"kind": "files", "op": "list"}, timeout=1))
        [question] = await hub.next(1, wait=0.5)
        assert not hub.answer(2, question["id"], {"entries": ["forged"]}), "another member"
        assert hub.answer(1, question["id"], {"entries": []})
        return await pending

    assert asyncio.run(go()) == {"entries": []}


def test_a_satellite_that_never_answers_times_out():
    async def go():
        return await Hub().ask(1, {"kind": "files"}, timeout=0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(go())


def test_an_answer_to_nothing_is_refused_by_the_route(server):  # noqa: F811
    r = server.post("/satellite/answer", json={"id": "q99", "answer": {}},
                    headers=bearer(TOKEN))
    assert r.status_code == 404


# ── The Tools ───────────────────────────────────────────────────────────────
class LaptopHub(Hub):
    """A Hub whose Satellite answers at once, from real files."""

    def __init__(self, folders, online=True):
        super().__init__()
        self.folders, self.online, self.asked = folders, online, []

    def connected(self, member_id):
        return self.online

    async def ask(self, member_id, request, timeout=30.0):
        self.asked.append(member_id)
        return satellite_files.handle(request, self.folders)


class DocChannel:
    def __init__(self):
        self.documents = []

    async def send_document(self, conversation_id, filename, data, caption=""):
        self.documents.append((conversation_id, filename, data))
        return "m1"


@pytest.fixture
def ctx(tmp_path, home):
    _, folders = home

    def build(in_group=False, online=True, member_id=1):
        turn = Turn(member_id=member_id, conversation_id="c1", in_group=in_group,
                    permissions=OWNER_PERMISSIONS)
        return builtin_tools.ToolContext(
            conn=db.connect(tmp_path / "t.db"), turn=turn, channel="telegram",
            services={"hub": LaptopHub(folders, online), "channel": DocChannel()})
    return build


def run(c, name, **args):
    return asyncio.run(run_tool(registered()[name], c.turn, c, args))


def test_reading_a_file_taints_the_turn(ctx):
    """A file can hold anybody's words (a download, a README): D27 applies."""
    c = ctx()
    out = run(c, "files_read", path="contas/luz.txt")
    assert "R$ 120" in out.text and c.turn.tainted


def test_a_file_is_sent_to_the_askers_chat(ctx):
    c = ctx()
    out = run(c, "files_send", path="manual.pdf")
    [(conv, name, data)] = c.services["channel"].documents
    assert (conv, name) == ("c1", "manual.pdf") and data.startswith(b"%PDF")
    assert "sent" in out.text


def test_never_from_a_group(ctx):
    c = ctx(in_group=True)
    assert "private" in run(c, "files_list").text
    assert c.services["hub"].asked == []


def test_an_offline_laptop_is_said_not_waited_for(ctx):
    c = ctx(online=False)
    assert "not connected" in run(c, "files_read", path="contas/luz.txt").text


def test_each_member_reaches_only_their_own_computer(ctx):
    c = ctx(member_id=2)
    run(c, "files_list")
    assert c.services["hub"].asked == [2]


def test_telegram_sends_the_file_as_multipart():
    from ta.channel.telegram import TelegramChannel

    seen = []

    def api(request):
        seen.append((request.url.path, request.headers["content-type"], request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    ch = TelegramChannel("123:abc", transport=httpx.MockTransport(api))
    assert asyncio.run(ch.send_document("c1", "manual.pdf", b"%PDF-1.4")) == "42"
    path, ctype, body = seen[0]
    assert path.endswith("/sendDocument") and ctype.startswith("multipart/form-data")
    assert b'filename="manual.pdf"' in body and b"%PDF-1.4" in body
