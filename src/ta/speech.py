"""Speech to text, on this machine (D2): audio never leaves the house.

faster-whisper is an **optional** extra (`pip install -e ".[voice]"`). It pulls in
CTranslate2 and PyAV, which a laptop that only types notes does not need, and the
rest of the project must not notice its absence — the `voice` Capability says
whether it is there.

Two limits come from the server it was sized for (an i5 of the 6th generation,
8 GB shared with Home Assistant, PLAN "Hardware budget"):

- **One transcription at a time.** The `small` model takes about a gigabyte while
  it runs; two voice notes arriving together must queue, not double that.
- **A ceiling on duration.** Transcription costs a fraction of the audio's length
  on this CPU, so a 40-minute recording would hold the only slot for a quarter of
  an hour. Longer audio is kept and captured as a placeholder instead.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

log = logging.getLogger("ta.speech")

DEFAULT_MODEL = "small"
DEFAULT_COMPUTE = "int8"
DEFAULT_MAX_SECONDS = 600


class TranscriptionFailed(RuntimeError):
    """The audio could not be turned into text. The message is for a log."""


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


class Transcriber:
    def __init__(
        self,
        model: str | None = None,
        compute_type: str | None = None,
        max_seconds: int | None = None,
    ) -> None:
        self.model_name = model or os.environ.get("TA_WHISPER_MODEL", DEFAULT_MODEL)
        self.compute_type = compute_type or os.environ.get(
            "TA_WHISPER_COMPUTE", DEFAULT_COMPUTE
        )
        self.max_seconds = max_seconds or int(
            os.environ.get("TA_WHISPER_MAX_SECONDS", DEFAULT_MAX_SECONDS)
        )
        self._model = None
        self._slot = asyncio.Semaphore(1)

    @property
    def available(self) -> bool:
        return available()

    def _load(self):
        # Lazily, on the first voice note: loading costs seconds and the RAM,
        # and a daemon nobody sends audio to should pay neither. The first load
        # also downloads the model (hundreds of MB) into the Hugging Face cache.
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper %s (%s)", self.model_name, self.compute_type)
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type
            )
        return self._model

    def _run(self, audio: bytes, language: str) -> str:
        model = self._load()
        # `segments` is a generator: the work happens while it is consumed.
        # The VAD filter drops silence, which is most of a hesitant voice note.
        segments, _ = model.transcribe(io.BytesIO(audio), language=language, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, audio: bytes, *, language: str) -> str:
        if not self.available:
            raise TranscriptionFailed("faster-whisper is not installed")
        async with self._slot:
            try:
                text = await asyncio.to_thread(self._run, audio, language)
            except Exception as e:
                raise TranscriptionFailed(f"{type(e).__name__}: {e}") from e
        # Stripped here and not only in `_run`: a blank transcript would become
        # an empty Note, which is worse than the placeholder that keeps the audio.
        text = text.strip()
        if not text:
            raise TranscriptionFailed("the audio held no speech")
        return text
