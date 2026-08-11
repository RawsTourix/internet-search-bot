"""API package composition hooks for internal transport routes and lifecycle."""

from __future__ import annotations

import inspect
import os
import sys

from dotenv import load_dotenv

from src.api import output_outbox_routes as _output_routes
from src.api.emission_outbox_routes import add_emission_outbox_routes
from src.api.runtime_diagnostics_routes import add_runtime_diagnostics_routes


if not getattr(_output_routes, "_ir6_emission_routes_installed", False):
    _base_create_output_outbox_router = _output_routes.create_output_outbox_router
    _base_signature = inspect.signature(_base_create_output_outbox_router)

    def _create_output_and_emission_outbox_router(*args, **kwargs):
        bound = _base_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        router = _base_create_output_outbox_router(*args, **kwargs)
        add_emission_outbox_routes(
            router,
            auth_dependency=bound.arguments["auth_dependency"],
            api_key_scopes=bound.arguments["api_key_scopes"],
            api_key_instance_scopes=bound.arguments.get("api_key_instance_scopes"),
        )
        facade = bound.arguments["facade"]
        if getattr(facade.api, "input_runtime_diagnostics", None) is not None:
            add_runtime_diagnostics_routes(
                router,
                facade=facade,
                auth_dependency=bound.arguments["auth_dependency"],
                api_key_scopes=bound.arguments["api_key_scopes"],
            )
        return router

    _output_routes.create_output_outbox_router = (
        _create_output_and_emission_outbox_router
    )
    _output_routes._ir6_emission_routes_installed = True


def ensure_input_runtime_projection_compatibility() -> bool:
    """Install the IR-9 MessageProcessor projection hook when composition exists."""

    return False


# Keep importing `src.api.config`, artifact routes, etc. side-effect free when
# no agent composition is configured. Production uses the same .env loading as
# api.config. The projection hook is special: MessageProcessor-first imports can
# enter this package while that module is only partially initialized, so the
# hook is deferred to API.start instead of touching a half-built class.
load_dotenv()
if (os.getenv("AGENT_CONFIG_PATH") or "").strip():
    from src.api import api as _api_module  # noqa: E402
    from src.api import input_runtime_recovery as _ir8_lifecycle  # noqa: E402
    from src.mcp.ir9_projection_checkpoints import (  # noqa: E402
        install_ir9_projection_checkpoint_hook,
    )
    from src.api.input_runtime_diagnostics import (  # noqa: E402
        install_input_runtime_diagnostics,
    )
    from src.api.input_runtime_projection_compatibility import (  # noqa: E402
        install_input_runtime_projection_compatibility,
    )
    from src.api.input_runtime_recovery_composition import (  # noqa: E402
        install_production_recovery_types,
    )

    install_production_recovery_types()
    _ir8_lifecycle.install_input_runtime_recovery_lifecycle(_api_module)
    install_ir9_projection_checkpoint_hook()
    install_input_runtime_diagnostics(_api_module.API)

    def ensure_input_runtime_projection_compatibility() -> bool:
        install_input_runtime_projection_compatibility(_api_module.API)
        return bool(
            getattr(
                _api_module.API,
                "_ir9_projection_compatibility_installed",
                False,
            )
        )

    _message_processor_module = sys.modules.get("src.core.message_processor")
    _message_processor_is_partial = (
        _message_processor_module is not None
        and not hasattr(_message_processor_module, "MessageProcessor")
    )
    if _message_processor_is_partial:
        if not getattr(
            _api_module.API,
            "_ir9_projection_startup_deferred",
            False,
        ):
            _base_start = _api_module.API.start

            async def _start_with_projection_compatibility(*args, **kwargs):
                ensure_input_runtime_projection_compatibility()
                return await _base_start(*args, **kwargs)

            _api_module.API.start = _start_with_projection_compatibility
            _api_module.API._ir9_projection_startup_deferred = True
    else:
        ensure_input_runtime_projection_compatibility()
