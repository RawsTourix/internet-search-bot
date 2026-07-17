"""Managed errors raised by the memory/result-compaction layer."""


class MemoryLayerError(RuntimeError):
    """Base class for managed memory-layer failures."""


class InvalidResultHandlingError(MemoryLayerError):
    """The agent supplied an unsupported result-handling preference."""


class MemoryConfigValidationError(MemoryLayerError):
    """The result-compaction configuration is invalid."""


class ResultCompactionError(MemoryLayerError):
    """A stored result could not be represented as requested."""
