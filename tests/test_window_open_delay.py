"""Verzoegerung beim Oeffnen des Fensters (`window_open_delay`).

Manche Fenstersensoren melden "gekippt" und "offen" nacheinander, mit
Verzoegerung. Ohne Wartezeit faehrt der Rollladen auf die erste Meldung und
gleich darauf auf die zweite. Mit Wartezeit wird der Kontakt am Ende neu
gelesen und der Rollladen faehrt einmal.

Die Wartezeit startet mit der ersten Meldung. Ein Wechsel zwischen "gekippt"
und "offen" startet sie nicht neu, nur "geschlossen" bricht sie ab.

Die Wartezeit liegt bewusst bei 7 s und damit neben der Entprellung (5 s):
`_gated_sleep()` haelt nur genau eine Dauer an. Die Entprellung steht in den
Tests auf 0, damit "geschlossen" synchron ausgewertet wird und kein zweiter,
echter Schlaf entsteht.
"""

from __future__ import annotations

import pytest

from custom_components.shutter_pilot import window_trigger
from custom_components.shutter_pilot.const import (
    CONF_WINDOW_CLOSE_DEBOUNCE,
    CONF_WINDOW_OPEN_DELAY,
)
from tests.test_window_debounce import (  # noqa: F401 - cover_calls ist ein Fixture
    COVER,
    _gated_sleep,
    _positions,
    _setup,
    _shutter,
    _window,
    _window_bounce,
    cover_calls,
)

DELAY = 7


def _delayed(**overrides) -> dict:
    return _shutter(
        **{CONF_WINDOW_OPEN_DELAY: DELAY, CONF_WINDOW_CLOSE_DEBOUNCE: 0, **overrides}
    )


class TestWait:
    async def test_drives_only_after_the_delay(self, hass, cover_calls):
        _, data = await _setup(hass, _delayed())

        with _gated_sleep(DELAY) as gate:
            await _window_bounce(hass, "tilted")
            assert _positions(cover_calls) == [], "noch nichts gefahren"
            assert COVER in data["_window_open_tasks"]
            gate.set()
            await hass.async_block_till_done()

        assert _positions(cover_calls) == [50]
        assert not data.get("_window_open_tasks"), "Task raeumt sich auf"

    async def test_tilted_then_open_drives_once_to_open(self, hass, cover_calls):
        """Der gemeldete Fall: erst "gekippt", kurz darauf "offen"."""
        _, data = await _setup(hass, _delayed())

        with _gated_sleep(DELAY) as gate:
            await _window_bounce(hass, "tilted")
            first = data["_window_open_tasks"][COVER]
            await _window_bounce(hass, "open")
            assert data["_window_open_tasks"][COVER] is first, "Wartezeit nicht neu gestartet"
            assert _positions(cover_calls) == []
            gate.set()
            await hass.async_block_till_done()

        assert _positions(cover_calls) == [100], "genau eine Fahrt, auf offen"

    async def test_open_then_tilted_drives_once_to_tilted(self, hass, cover_calls):
        """Auch andersherum: der Zustand am Ende der Wartezeit zaehlt."""
        _, data = await _setup(hass, _delayed())

        with _gated_sleep(DELAY) as gate:
            await _window_bounce(hass, "open")
            first = data["_window_open_tasks"][COVER]
            await _window_bounce(hass, "tilted")
            assert data["_window_open_tasks"][COVER] is first
            gate.set()
            await hass.async_block_till_done()

        assert _positions(cover_calls) == [50]


class TestZeroDelay:
    async def test_behaves_exactly_like_before(self, hass, cover_calls):
        """0 s (Vorgabe) bleibt synchron: kein Task, keine andere Reihenfolge."""
        _, data = await _setup(hass, _shutter(**{CONF_WINDOW_CLOSE_DEBOUNCE: 0}))

        with _gated_sleep(DELAY) as gate:
            await _window(hass, "tilted")
            assert _positions(cover_calls) == [50], "sofort gefahren"
            assert not gate.is_set()
        assert not data.get("_window_open_tasks")


class TestCancelledByClosed:
    async def test_closed_during_the_wait_drops_it(self, hass, cover_calls):
        _, data = await _setup(hass, _delayed())

        with _gated_sleep(DELAY) as gate:
            await _window_bounce(hass, "tilted")
            task = data["_window_open_tasks"][COVER]
            await _window_bounce(hass, "closed")
            assert COVER not in data.get("_window_open_tasks", {})
            gate.set()
            await hass.async_block_till_done()

        assert task.cancelled()
        assert _positions(cover_calls) == [], "weder auf noch zurueck gefahren"

    async def test_state_closed_again_without_an_event(self, hass, cover_calls):
        """Nach dem Schlaf wird der Zustand neu gelesen, nicht geglaubt."""
        _, data = await _setup(hass, _delayed())

        with _gated_sleep(DELAY) as gate:
            await _window_bounce(hass, "tilted")
            # Listener abbestellen: es kommt kein Event an, als einzige
            # Sicherung bleibt die erneute Abfrage nach dem Schlaf.
            for unsub in data["_window_unsubs"]:
                unsub()
            data["_window_unsubs"] = []
            hass.states.async_set("sensor.living_room_window", "closed")
            gate.set()
            await hass.async_block_till_done()

        assert _positions(cover_calls) == []


class TestCancellation:
    async def test_unload_cancels(self, hass, cover_calls):
        with _gated_sleep(DELAY):
            entry, data = await _setup(hass, _delayed())
            await _window_bounce(hass, "tilted")
            task = data["_window_open_tasks"][COVER]

            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

        assert task.cancelled() or task.done()
        assert _positions(cover_calls) == []

    async def test_reload_cancels(self, hass, cover_calls):
        """Sonst fuehre ein Timer die Konfiguration von vor dem Speichern aus."""
        with _gated_sleep(DELAY):
            entry, data = await _setup(hass, _delayed())
            await _window_bounce(hass, "tilted")
            task = data["_window_open_tasks"][COVER]

            await window_trigger.setup_window_triggers(hass, entry)
            await hass.async_block_till_done()

        assert task.cancelled() or task.done()
        assert not data.get("_window_open_tasks")

    async def test_automation_off_cancels(self, hass, cover_calls):
        """Ein abgeschalteter Rollladen darf nicht doch noch fahren."""
        with _gated_sleep(DELAY):
            _, data = await _setup(hass, _delayed())
            await _window_bounce(hass, "tilted")
            task = data["_window_open_tasks"][COVER]

            data["shutter_automation"] = {COVER: False}
            # Irgendein Fenster-Event: der Automatik-Zweig greift, bevor der
            # Zustand ausgewertet wird.
            await _window_bounce(hass, "open")

        await hass.async_block_till_done()
        assert task.cancelled()
        assert _positions(cover_calls) == []


class TestClamping:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (-5, 0),
            (0, 0),
            (7, 7),
            (30, 30),
            (999, 30),
            ("abc", 0),
            (None, 0),
            ("", 0),
        ],
    )
    def test_clamped(self, value, expected):
        assert window_trigger._open_delay_seconds({CONF_WINDOW_OPEN_DELAY: value}) == expected

    def test_missing_key_means_off(self):
        assert window_trigger._open_delay_seconds({}) == 0
