"""Managed errors raised by the memory/result-compaction layer."""


class MemoryLayerError(RuntimeError):
    """Base class for managed memory-layer failures."""


class InvalidResultHandlingError(MemoryLayerError):
    """The agent supplied an unsupported result-handling preference."""


class MemoryConfigValidationError(MemoryLayerError):
    """The result-compaction configuration is invalid."""


class ResultCompactionError(MemoryLayerError):
    """A stored result could not be represented as requested."""


class CycleCompactionError(MemoryLayerError):
    """Base class for managed active-cycle compaction failures."""


class CycleCompactionOutputError(CycleCompactionError):
    """The internal compactor returned an invalid structured result."""


class CycleSegmentSelectionError(CycleCompactionError):
    """The visible message history is unsafe for atomic replacement."""


class CycleContextLimitError(CycleCompactionError):
    """The cycle cannot safely continue within the hard context limit."""
