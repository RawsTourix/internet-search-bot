"""Public API for result-compaction memory services."""

from .config import MemoryConfigType
from .context_budget import ResultContextBudgetPolicy
from .errors import (
    InvalidResultHandlingError,
    MemoryConfigValidationError,
    MemoryLayerError,
    ResultCompactionError,
)
from .models import (
    ResultBudgetDecision,
    ResultCompactionRequest,
    ResultCompactionSummary,
    ResultHandling,
    ResultProcessingOutcome,
)
from .result_compaction import (
    RESULT_COMPACTION_SYSTEM_PROMPT,
    ResultCompactionService,
)

__all__ = [
    "InvalidResultHandlingError",
    "MemoryConfigType",
    "MemoryConfigValidationError",
    "MemoryLayerError",
    "ResultBudgetDecision",
    "ResultCompactionError",
    "ResultCompactionRequest",
    "ResultCompactionService",
    "ResultCompactionSummary",
    "ResultContextBudgetPolicy",
    "ResultHandling",
    "ResultProcessingOutcome",
    "RESULT_COMPACTION_SYSTEM_PROMPT",
]
