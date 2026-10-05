"""Every source vpop knows about. A new source is added here."""

from vpop.config import Config
from vpop.sources.android_messages.source import AndroidMessages
from vpop.sources.base import Source

SOURCES: tuple[type[Source], ...] = (AndroidMessages,)


def enabled_sources(config: Config) -> list[Source]:
    """The sources the config turns on, in `SOURCES` order."""
    sources = [cls(config) for cls in SOURCES]
    return [source for source in sources if source.enabled()]
