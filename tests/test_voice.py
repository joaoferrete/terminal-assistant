"""Voice notes (F2): transcribed on this machine, and never lost (invariant 1).

faster-whisper is an optional extra and the suite does not need it: the
transcriber is a fake. What is under test is what happens around it — and above
all that every way it can fail still ends as a Note.
"""
import asyncio
import json
import types

import httpx
import pytest
from test_bot import FakeChannel, inbound

from ta import db
from ta.bot import Bot
from ta.channel import ChannelError
from ta.channel.telegram import TelegramChannel
from ta.speech import Transcriber, TranscriptionFailed

AUDIO = b"OggS-fake-audio"


class VoiceChannel(FakeChannel):
    def __init__(self, audio=AUDIO, fails=False):
        super().__init__()
        self.audio, self.fails = audio, fails

    async def download(self, file_id):
        if self.fails:
            raise ChannelError("telegram download: ConnectError")
        return self.audio


class FakeTranscriber:
    available = True
    max_seconds = 600

    def __init__(self, text="lembrar de pagar o IPTU", fails=False):
        self.text, self.fails, self.calls = text, fails, []

    async def transcribe(self, audio, *, language):
        self.calls.append((audio, language))
        if self.fails:
            raise TranscriptionFailed("boom")
        return self.text


def voice(seconds=8):
    return inbound("", unsupported=False).__class__(
        **{**inbound("").__dict__, "voice_file_id": "f1", "voice_seconds": seconds}
    )


def make_bot(tmp_path, channel=None, transcriber=None):
    captured = []

    def capture(text, owner_id=1):
        captured.append(text)
        return types.SimpleNamespace(id=len(captured), due=None)

    b = Bot(db.connect(tmp_path / "t.db"), channel or VoiceChannel(), capture=capture,
            owner_username="dono", transcriber=transcriber, audio_dir=tmp_path / "audio")
    b.captured = captured
    return b


def run_voice(b, msg):
    async def go():
        await b.handle(inbound("/start"))   # pair first
        await b.handle(msg)
        await asyncio.gather(*b._voice_tasks)

    asyncio.run(go())


def test_a_voice_note_becomes_a_note_with_its_transcript(tmp_path):
    tr = FakeTranscriber()
    b = make_bot(tmp_path, transcriber=tr)
    run_voice(b, voice())
    assert b.captured == ["lembrar de pagar o IPTU"]
    assert tr.calls == [(AUDIO, "pt")], "the language follows the installation (ADR 0013)"
    assert "lembrar de pagar o IPTU" in b.channel.sent[-1], "the user sees what was heard"
    assert not (tmp_path / "audio").exists(), "a transcribed note keeps no audio"


def keeps_it(b, tmp_path):
    assert len(b.captured) == 1, "invariant 1: it still became a Note"
    kept = list((tmp_path / "audio").glob("*.ogg"))
    assert len(kept) == 1 and kept[0].read_bytes() == AUDIO
    assert str(kept[0]) in b.captured[0], "the placeholder says where the audio is"


def test_a_failed_transcription_keeps_the_audio_and_still_captures(tmp_path):
    b = make_bot(tmp_path, transcriber=FakeTranscriber(fails=True))
    run_voice(b, voice())
    keeps_it(b, tmp_path)


def test_without_whisper_installed_the_audio_is_kept(tmp_path):
    tr = FakeTranscriber()
    tr.available = False
    b = make_bot(tmp_path, transcriber=tr)
    run_voice(b, voice())
    keeps_it(b, tmp_path)
    assert tr.calls == []


def test_audio_past_the_ceiling_is_kept_not_transcribed(tmp_path):
    """One slot on a small CPU: a 40-minute recording would hold it for ages."""
    tr = FakeTranscriber()
    b = make_bot(tmp_path, transcriber=tr)
    run_voice(b, voice(seconds=601))
    keeps_it(b, tmp_path)
    assert tr.calls == []


def test_a_failed_download_still_leaves_a_note(tmp_path):
    b = make_bot(tmp_path, channel=VoiceChannel(fails=True), transcriber=FakeTranscriber())
    run_voice(b, voice())
    assert len(b.captured) == 1
    assert not (tmp_path / "audio").exists(), "there were no bytes to keep"


def test_a_voice_note_does_not_hold_up_the_texts_behind_it(tmp_path):
    """Transcription runs off the Channel's loop; `handle` returns at once."""
    gate = asyncio.Event()

    class Slow(FakeTranscriber):
        async def transcribe(self, audio, *, language):
            await gate.wait()
            return "devagar"

    b = make_bot(tmp_path, transcriber=Slow())

    async def go():
        await b.handle(inbound("/start"))
        await b.handle(voice())
        await b.handle(inbound("texto depois do áudio"))
        assert b.captured == ["texto depois do áudio"]
        gate.set()
        await asyncio.gather(*b._voice_tasks)

    asyncio.run(go())
    assert b.captured == ["texto depois do áudio", "devagar"]


# ── The real Transcriber, without the model ─────────────────────────────────
def test_the_transcriber_fails_cleanly_when_the_extra_is_missing(monkeypatch):
    monkeypatch.setattr("ta.speech.available", lambda: False)
    with pytest.raises(TranscriptionFailed):
        asyncio.run(Transcriber().transcribe(AUDIO, language="pt"))


def test_empty_speech_is_a_failure_not_an_empty_note(monkeypatch):
    t = Transcriber()
    monkeypatch.setattr("ta.speech.available", lambda: True)
    monkeypatch.setattr(t, "_run", lambda audio, language: "  ")
    with pytest.raises(TranscriptionFailed):
        asyncio.run(t.transcribe(AUDIO, language="pt"))


def test_the_model_is_configured_from_the_environment(monkeypatch):
    monkeypatch.setenv("TA_WHISPER_MODEL", "base")
    monkeypatch.setenv("TA_WHISPER_MAX_SECONDS", "90")
    t = Transcriber()
    assert (t.model_name, t.compute_type, t.max_seconds) == ("base", "int8", 90)


# ── Telegram's download ─────────────────────────────────────────────────────
def test_download_goes_through_getfile_and_the_file_url():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "f1"}
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/a.oga"}})
        return httpx.Response(200, content=AUDIO)

    ch = TelegramChannel("123:SECRET", transport=httpx.MockTransport(handler))
    assert asyncio.run(ch.download("f1")) == AUDIO
    assert paths[1] == "/file/bot123:SECRET/voice/a.oga"


def test_a_failed_download_never_carries_the_token():
    def handler(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "x"}})
        return httpx.Response(404)

    ch = TelegramChannel("123:SECRET", transport=httpx.MockTransport(handler))
    with pytest.raises(ChannelError) as e:
        asyncio.run(ch.download("f1"))
    assert "SECRET" not in str(e.value)
