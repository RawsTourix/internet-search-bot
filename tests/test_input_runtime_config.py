import json
from pathlib import Path

import pytest

from src.input_runtime.config import (
    InputRuntimeConfigType,
    load_input_runtime_config,
    safe_input_runtime_config_summary,
)
from src.input_runtime.errors import InputRuntimeConfigValidationError


def test_defaults_match_canonical_contract():
    config = InputRuntimeConfigType()
    assert config.enabled is True
    assert config.max_queued_batches_per_session == 64
    assert config.max_queued_bytes_per_session == 268435456
    assert config.max_batches_per_checkpoint == 8
    assert config.max_batch_bytes_per_checkpoint == 67108864
    assert config.claim_lease_seconds == 300
    assert config.max_intermediate_messages_per_cycle == 16
    assert config.min_intermediate_message_interval_seconds == 15.0
    assert config.max_intermediate_message_chars == 3500


def test_config_rejects_unknown_and_impossible_relations():
    with pytest.raises(Exception):
        InputRuntimeConfigType(unknown=True)
    with pytest.raises(Exception):
        InputRuntimeConfigType(max_queued_batches_per_session=2, max_batches_per_checkpoint=3)
    with pytest.raises(Exception):
        InputRuntimeConfigType(min_intermediate_message_interval_seconds=float("nan"))


def test_loader_and_safe_summary(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"input_runtime": {"claim_lease_seconds": 42}}), encoding="utf-8")
    config = load_input_runtime_config(path)
    assert config.claim_lease_seconds == 42
    assert safe_input_runtime_config_summary(config) == config.model_dump(mode="json")


def test_loader_rejects_non_object_section(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text('{"input_runtime": []}', encoding="utf-8")
    with pytest.raises(InputRuntimeConfigValidationError):
        load_input_runtime_config(path)


def test_canonical_example_contains_every_input_runtime_setting():
    payload = json.loads(Path("src/api/mcp.config.example").read_text(encoding="utf-8"))
    section = payload["input_runtime"]
    assert set(section) == set(InputRuntimeConfigType.model_fields)
    assert InputRuntimeConfigType.model_validate(section)
