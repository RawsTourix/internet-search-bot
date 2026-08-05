"""Managed input-runtime errors."""
from __future__ import annotations
from typing import Any

class InputRuntimeError(RuntimeError):
    def __init__(self, safe_message: str, *, code: str = "input_runtime_error", retryable: bool = False, details: dict[str, Any] | None = None) -> None:
        super().__init__(safe_message)
        self.code=code; self.safe_message=safe_message; self.retryable=retryable; self.details=dict(details or {})
class InputRuntimeConfigValidationError(InputRuntimeError): pass
class InputRuntimeValidationError(InputRuntimeError): pass
class InputRuntimeNotFoundError(InputRuntimeError): pass
class InputRuntimeConflictError(InputRuntimeError): pass
class InputRuntimeStorageError(InputRuntimeError): pass
class InputRuntimeCapacityError(InputRuntimeError): pass
class InputRuntimeClaimError(InputRuntimeError): pass
class InputRuntimeConsistencyError(InputRuntimeError): pass
class InputRuntimeStaleGenerationError(InputRuntimeConflictError): pass
class InputRuntimeFinalizationError(InputRuntimeError): pass
