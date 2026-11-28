import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_layerwise_config.py"
spec = importlib.util.spec_from_file_location("check_layerwise_config", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _cfg(store_extra, p2p_extra=None):
    return {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
            "connectors": [
                {
                    "kv_connector": "MooncakeLayerwiseConnector",
                    "kv_role": "kv_producer",
                    "kv_connector_extra_config": p2p_extra or {},
                },
                {
                    "kv_connector": "AscendStoreConnector",
                    "kv_role": "kv_producer",
                    "kv_connector_extra_config": store_extra,
                },
            ],
        },
    }


def test_mode_a_valid_config():
    errors, _, notes = mod.check(
        _cfg({"backend": "mooncake", "load_async": False, "use_layerwise": False})
    )
    assert errors == []
    assert notes


def test_store_use_layerwise_is_out_of_scope():
    errors, _, _ = mod.check(_cfg({"backend": "mooncake", "load_async": False, "use_layerwise": True}))
    assert any("outside public Mode A" in e for e in errors)


def test_store_use_layerwise_string_is_error():
    errors, _, _ = mod.check(
        _cfg({"backend": "mooncake", "load_async": False, "use_layerwise": "false"})
    )
    assert any("use_layerwise must be JSON boolean" in e for e in errors)


def test_load_async_string_is_error():
    errors, _, _ = mod.check(_cfg({"backend": "mooncake", "load_async": "false", "use_layerwise": False}))
    assert any("load_async must be JSON boolean" in e for e in errors)


def test_internal_fields_are_rejected():
    errors, _, _ = mod.check(
        {
            "kv_connector": "MultiConnector",
            "kv_role": "kv_producer",
            "kv_connector_extra_config": {
                "layerwise_host_kv_offload": True,
                "connectors": [
                    {
                        "kv_connector": "MooncakeLayerwiseConnector",
                        "kv_connector_extra_config": {"use_ascend_direct": True},
                    },
                    {
                        "kv_connector": "AscendStoreConnector",
                        "kv_connector_extra_config": {
                            "use_layerwise": True,
                            "use_layerwise_range": True,
                        },
                    },
                ],
            },
        }
    )
    assert any("layerwise_host_kv_offload" in e for e in errors)
    assert any("use_layerwise_range" in e for e in errors)
    assert any("use_ascend_direct" in e for e in errors)


def test_missing_layerwise_child_is_error():
    cfg = _cfg({"backend": "mooncake", "load_async": False, "use_layerwise": False})
    cfg["kv_connector_extra_config"]["connectors"] = cfg["kv_connector_extra_config"]["connectors"][1:]
    errors, _, _ = mod.check(cfg)
    assert any("requires a MooncakeLayerwiseConnector" in e for e in errors)


def test_missing_file_is_structured_error():
    import pytest

    with pytest.raises(ValueError, match="readable JSON file"):
        mod.load_config("/nonexistent/layerwise-config.json")


def test_invalid_json_string_is_structured_error():
    import pytest

    with pytest.raises(ValueError, match="readable JSON file"):
        mod.load_config("{bad json")
