import json
from pathlib import Path

import pytest

from scripts.audit_configuration_examples import (
    CONFIG_SECTION_MODELS, missing_config_example_fields,
    missing_config_example_sections, unregistered_config_sections,
    validate_config_example_values,
)
from src.input_runtime.config import (
    InputRuntimeConfigType, load_input_runtime_config,
    parse_input_runtime_config, safe_input_runtime_config_summary,
)
from src.input_runtime.errors import InputRuntimeConfigValidationError


def test_defaults_match_canonical_contract():
    config = InputRuntimeConfigType()
    assert config.model_dump() == {
        "enabled": True,
        "max_queued_batches_per_session": 64,
        "max_queued_bytes_per_session": 268435456,
        "max_batches_per_checkpoint": 8,
        "max_batch_bytes_per_checkpoint": 67108864,
        "claim_lease_seconds": 300,
        "max_intermediate_messages_per_cycle": 16,
        "min_intermediate_message_interval_seconds": 15.0,
        "max_intermediate_message_chars": 3500,
    }


@pytest.mark.parametrize("field", [
    "max_queued_batches_per_session", "max_queued_bytes_per_session",
    "max_batches_per_checkpoint", "max_batch_bytes_per_checkpoint",
    "claim_lease_seconds", "max_intermediate_messages_per_cycle",
    "max_intermediate_message_chars",
])
@pytest.mark.parametrize("value", [0, -1])
def test_positive_integer_limits(field, value):
    with pytest.raises(Exception): InputRuntimeConfigType(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_interval_rejects_nonfinite_and_negative(value):
    with pytest.raises(Exception): InputRuntimeConfigType(min_intermediate_message_interval_seconds=value)


def test_unknown_and_impossible_relations_rejected():
    with pytest.raises(Exception): InputRuntimeConfigType(unknown=True)
    with pytest.raises(Exception): InputRuntimeConfigType(max_queued_batches_per_session=2, max_batches_per_checkpoint=3)
    with pytest.raises(Exception): InputRuntimeConfigType(max_queued_bytes_per_session=2, max_batch_bytes_per_checkpoint=3)


def test_parse_none_means_missing_optional_section_defaults():
    assert parse_input_runtime_config(None) == InputRuntimeConfigType()


def test_loader_missing_section_uses_defaults(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    assert load_input_runtime_config(path) == InputRuntimeConfigType()


def test_loader_null_section_is_rejected(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text('{"input_runtime": null}', encoding="utf-8")
    with pytest.raises(InputRuntimeConfigValidationError): load_input_runtime_config(path)


@pytest.mark.parametrize("payload", ["[]", "null", '"text"', "1"])
def test_root_json_must_be_object(tmp_path: Path, payload):
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(InputRuntimeConfigValidationError) as caught:
        load_input_runtime_config(path)
    assert caught.value.__cause__ is not None


def test_malformed_json_has_managed_error_and_cause(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(InputRuntimeConfigValidationError) as caught:
        load_input_runtime_config(path)
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_missing_file_has_managed_error_and_cause(tmp_path: Path):
    with pytest.raises(InputRuntimeConfigValidationError) as caught:
        load_input_runtime_config(tmp_path / "missing.json")
    assert isinstance(caught.value.__cause__, OSError)


def test_loader_and_safe_summary(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"input_runtime": {"claim_lease_seconds": 42}}), encoding="utf-8")
    config = load_input_runtime_config(path)
    assert config.claim_lease_seconds == 42
    assert safe_input_runtime_config_summary(config) == config.model_dump(mode="json")


def test_canonical_example_and_common_audit_cover_input_runtime():
    payload = json.loads(Path("src/api/mcp.config.example").read_text(encoding="utf-8"))
    assert CONFIG_SECTION_MODELS["input_runtime"] is InputRuntimeConfigType
    assert set(payload["input_runtime"]) == set(InputRuntimeConfigType.model_fields)
    assert missing_config_example_sections() == []
    assert unregistered_config_sections() == []
    assert missing_config_example_fields() == {}
    validate_config_example_values()
