"""Speaker volume that can be changed while Smitty runs (dashboard slider or by voice).

The level (0..1) is kept in the store, so it survives restarts and applies again
whenever the microphone and speaker are re-attached.
"""

from __future__ import annotations

import logging
from typing import Callable

from smitty import store

log = logging.getLogger(__name__)


class Volume:
    def __init__(self, settings):
        self.settings = settings
        self.apply: Callable[[float], None] | None = None     # set by the voice link: updates the live speaker
        saved = store.get("volume")
        if isinstance(saved, (int, float)) and 0 <= saved <= 1:
            settings.volume = float(saved)

    @property
    def value(self) -> float:
        return float(self.settings.volume)

    def set(self, value: float, via: str = "dashboard") -> float:
        value = round(min(1.0, max(0.0, float(value))), 2)
        if value != self.value:
            log.info("volume -> %d%% (via %s)", round(value * 100), via)
        self.settings.volume = value
        store.put("volume", value)
        if self.apply is not None:
            try:
                self.apply(value)
            except Exception as e:
                log.debug("could not apply the volume: %s", e)
        return value
