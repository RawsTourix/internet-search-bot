import json
import pytest
from src.input_runtime.config import InputRuntimeConfigType, load_input_runtime_config
from src.input_runtime.errors import InputRuntimeConfigValidationError


def write(tmp_path, payload):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_missing_and_null_sections_use_defaults(tmp_path):
    assert load_input_runtime_config(write(tmp_path, {})) == InputRuntimeConfigType()
    assert load_input_runtime_config(write(tmp_path, {"input_runtime": None})) == InputRuntimeConfigType()


def test_full_config_and_cross_limits(tmp_path):
    config = load_input_runtime_config(write(tmp_path, {"input_runtime": {
        "max_queued_batches_per_session": 4,
        "max_batches_per_checkpoint": 2,
        "max_queued_bytes_per_session": 100,
        "max_batch_bytes_per_checkpoint": 50,
    }}))
    assert config.max_batches_per_checkpoint == 2
    with pytest.raises(InputRuntimeConfigValidationError):
        load_input_runtime_config(write(tmp_path, {"input_runtime": {
            "max_queued_batches_per_session": 1,
            "max_batches_per_checkpoint": 2,
        }}))


def test_invalid_shapes_values_json_and_missing_file(tmp_path):
    payloads = [
        [],
        {"input_runtime": []},
        {"input_runtime": {"claim_lease_seconds": 0}},
        {"input_runtime": {"claim_lease_seconds": float("inf")}},
        {"input_runtime": {"unknown": 1}},
    ]
    for payload in payloads:
        with pytest.raises(InputRuntimeConfigValidationError):
            load_input_runtime_config(write(tmp_path, payload))
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(InputRuntimeConfigValidationError):
        load_input_runtime_config(str(bad))
    with pytest.raises(InputRuntimeConfigValidationError):
        load_input_runtime_config(str(tmp_path / "missing.json"))
