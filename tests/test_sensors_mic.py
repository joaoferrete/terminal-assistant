from ta.sensors.mic import DEBOUNCE_S, MicWatcher, _input_streams


def node(media_class, app=None, node_name=None):
    props = {"media.class": media_class}
    if app:
        props["application.name"] = app
    if node_name:
        props["node.name"] = node_name
    return {"info": {"props": props}}


# ── Reading the graph ───────────────────────────────────────────────────────
def test_outside_a_call_there_is_no_input_stream():
    """The negative case verified on the real machine: sources only, no stream."""
    dump = [node("Audio/Source", node_name="alsa_input.pci-0000_04_00.6"), node("Audio/Sink")]
    assert _input_streams(dump) == []


def test_it_recognises_a_capture_stream():
    assert _input_streams([node("Stream/Input/Audio", app="Chromium")]) == ["Chromium"]


def test_it_ignores_gnomes_own_monitor():
    """gnome-shell shows up as Stream/Input/Audio and is not a meeting."""
    dump = [node("Stream/Input/Audio", app="gnome-shell"), node("Stream/Input/Audio", app="Zoom")]
    assert _input_streams(dump) == ["Zoom"]


def test_an_output_stream_does_not_count():
    assert _input_streams([node("Stream/Output/Audio", app="Spotify")]) == []


# ── Debounce ────────────────────────────────────────────────────────────────
class FakeWatcher(MicWatcher):
    """Replaces the PipeWire read with a script of canned answers."""

    def __init__(self, script):
        self.events = []
        super().__init__(self._record)
        self._script = list(script)
        self._bin = "/fake/pw-dump"

    async def _record(self, active, apps):
        self.events.append((active, apps))

    async def _read(self):
        return self._script.pop(0) if self._script else []


async def test_the_transition_only_lands_after_the_debounce():
    w = FakeWatcher([["Zoom"], ["Zoom"]])
    await w.tick(0.0)                  # sees it active, marks it pending
    assert w.events == []
    await w.tick(DEBOUNCE_S + 0.1)     # confirms
    assert w.events == [(True, ["Zoom"])]
    assert w.active


async def test_a_short_burst_does_not_fire():
    """A stream that opens and closes in a burst must not flash the light."""
    w = FakeWatcher([["Zoom"], []])
    await w.tick(0.0)
    await w.tick(0.5)                  # back to empty before the debounce
    assert w.events == []
    assert not w.active


async def test_the_transition_works_in_both_directions():
    w = FakeWatcher([["Zoom"], ["Zoom"], [], []])
    await w.tick(0.0)
    await w.tick(DEBOUNCE_S + 0.1)
    await w.tick(100.0)
    await w.tick(100.0 + DEBOUNCE_S + 0.1)
    assert w.events == [(True, ["Zoom"]), (False, [])]
    assert not w.active


class FailingRead(FakeWatcher):
    async def _read(self):
        return None


async def test_a_failed_read_does_not_invent_a_transition():
    """None means "I could not read", which is not "nothing is running"."""
    w = FailingRead([])
    w.active = True
    await w.tick(0.0)
    await w.tick(100.0)
    assert w.events == []
    assert w.active     # keeps the state; it does not conclude the call dropped
