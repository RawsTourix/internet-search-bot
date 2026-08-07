"""API package composition hooks for internal transport routes."""

from __future__ import annotations

from . import output_outbox_routes as _output_routes
from .emission_outbox_routes import add_emission_outbox_routes


if not getattr(_output_routes, "_ir6_emission_routes_installed", False):
    _base_create_output_outbox_router = _output_routes.create_output_outbox_router

    def _create_output_and_emission_outbox_router(*args, **kwargs):
        router = _base_create_output_outbox_router(*args, **kwargs)
        add_emission_outbox_routes(
            router,
            auth_dependency=kwargs["auth_dependency"],
            api_key_scopes=kwargs["api_key_scopes"],
            api_key_instance_scopes=kwargs.get("api_key_instance_scopes"),
        )
        return router

    _output_routes.create_output_outbox_router = (
        _create_output_and_emission_outbox_router
    )
    _output_routes._ir6_emission_routes_installed = True
