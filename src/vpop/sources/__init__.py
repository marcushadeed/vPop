"""Data sources vpop imports into its database."""


class SourceError(RuntimeError):
    """A source can't be synced as set up. The message says what to do."""
