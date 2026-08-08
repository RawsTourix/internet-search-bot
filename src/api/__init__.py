"""API package composition hooks for internal transport routes and lifecycle."""

from __future__ import annotations

import inspect
import os

from dotenv import load_dotenv

from . import output_outbox_routes as _output_routes
from .emission_outbox_routes import add_emission_outbox_routes


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
        return router

    _output_routes.create_output_outbox_router = (
        _create_output_and_emission_outbox_router
    )
    _output_routes._ir6_emission_routes_installed = True


# Keep importing `src.api.config`, artifact routes, etc. side-effect free when
# no agent composition is configured.  Production uses the same .env loading as
# api.config; with a real AGENT_CONFIG_PATH the direct `src.api.api` import is
# intercepted here and the class-level IR-8 lifecycle is installed before the
# package import returns.  Validation suites without agent config therefore do
# not instantiate the production singleton merely by importing an API helper.
load_dotenv()
if (os.getenv("AGENT_CONFIG_PATH") or "").strip():
    from . import api as _api_module  # noqa: E402
    from . import input_runtime_recovery as _ir8_lifecycle  # noqa: E402
    from ..input_runtime.recovery_hardening import (  # noqa: E402
        InputRuntimeRecoveryCoordinator as _ConservativeRecoveryCoordinator,
    )

    _ir8_lifecycle.InputRuntimeRecoveryCoordinator = _ConservativeRecoveryCoordinator
    _ir8_lifecycle.install_input_runtime_recovery_lifecycle(_api_module)
