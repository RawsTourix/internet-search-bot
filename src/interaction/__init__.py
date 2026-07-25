"""Transport-independent semantic client interaction runtime.

The package keeps imports lazy so ingress models can reference semantic parts
without creating an ingress/output-model import cycle.
"""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ClientResponseAnchor": (".anchors", "ClientResponseAnchor"),
    "ClientResponseAnchorCandidate": (".anchors", "ClientResponseAnchorCandidate"),
    "ClientResponseAnchorKind": (".anchors", "ClientResponseAnchorKind"),
    "ResponseAnchorSelector": (".anchors", "ResponseAnchorSelector"),
    "ClientCapabilityDeclaration": (".capabilities", "ClientCapabilityDeclaration"),
    "ClientCapabilityRegistry": (".capabilities", "ClientCapabilityRegistry"),
    "ClientCapabilitySnapshot": (".capabilities", "ClientCapabilitySnapshot"),
    "ClientCapabilitySnapshotRef": (".capabilities", "ClientCapabilitySnapshotRef"),
    "FileSystemCapabilitySnapshotStore": (
        ".capability_store",
        "FileSystemCapabilitySnapshotStore",
    ),
    "InteractionConfig": (".config", "InteractionConfig"),
    "ClientCapabilitiesConfig": (".config", "ClientCapabilitiesConfig"),
    "LocalizationConfigType": (".config", "LocalizationConfigType"),
    "InputPresentationConfig": (".config", "InputPresentationConfig"),
    "OutputRuntimeConfig": (".config", "OutputRuntimeConfig"),
    "TelegramOutputConfig": (".config", "TelegramOutputConfig"),
    "load_interaction_config": (".config", "load_interaction_config"),
    "ArtifactInputManifest": (".parts", "ArtifactInputManifest"),
    "ArtifactManifestItem": (".parts", "ArtifactManifestItem"),
    "ArtifactDeliverableProjection": (".parts", "ArtifactDeliverableProjection"),
    "InputPart": (".parts", "InputPart"),
    "InputBatchPresentationRef": (".presentation", "InputBatchPresentationRef"),
    "InputPresentationEvent": (".presentation", "InputPresentationEvent"),
    "PresentationAckPolicy": (".presentation", "PresentationAckPolicy"),
    "PresentationState": (".presentation", "PresentationState"),
    "PublicPresentationRef": (".presentation", "PublicPresentationRef"),
    "FileSystemInputPresentationStore": (
        ".presentation_store",
        "FileSystemInputPresentationStore",
    ),
    "InputPresentationCoordinator": (
        ".presentation_service",
        "InputPresentationCoordinator",
    ),
    "OutputBatch": (".output_models", "OutputBatch"),
    "OutputBatchKind": (".output_models", "OutputBatchKind"),
    "OutputBatchState": (".output_models", "OutputBatchState"),
    "OutputPart": (".output_models", "OutputPart"),
    "OutputDeliveryPlan": (".output_models", "OutputDeliveryPlan"),
    "OutputDeliveryReceipt": (".output_models", "OutputDeliveryReceipt"),
    "FileSystemOutputBatchStore": (".output_store", "FileSystemOutputBatchStore"),
    "build_ready_output_batch": (".output_store", "build_ready_output_batch"),
    "OutputBatchAssembler": (".output_service", "OutputBatchAssembler"),
    "CapabilityOutputRenderer": (".rendering", "CapabilityOutputRenderer"),
    "ClientOutputRenderer": (".rendering", "ClientOutputRenderer"),
    "ClientRenderContext": (".rendering", "ClientRenderContext"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
