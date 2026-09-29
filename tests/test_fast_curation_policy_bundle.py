"""Pure policy-bundle and current-version gate tests."""

from __future__ import annotations

import pytest

from neocortex.semantic.fast_curation_policy import (
    CalibrationParameters,
    FastCurationPolicy,
)
from neocortex.semantic.fast_curation_policy_bundle import (
    FastCurationPolicyBundle,
    default_calibrated_policy,
    load_calibrated_policy,
    validate_record_versions,
)
from neocortex.semantic.fast_curation_service import FastCurationConfig
from neocortex.semantic.semantic_config import compact_multilingual_text_model
from neocortex.semantic.fast_curation_prototypes import default_prototypes


FINGERPRINT = "a" * 64


def _bundle() -> FastCurationPolicyBundle:
    model_signature = compact_multilingual_text_model().model_signature
    calibration = CalibrationParameters(
        "cal-v1",
        model_signature,
        True,
        {"document_kind": 0.70},
        {"document_kind": 0.10},
    )
    policy = FastCurationPolicy.from_calibration(calibration)
    return FastCurationPolicyBundle(
        policy,
        model_signature,
        "fast-curation-document-representation-v1",
        "fixture-v1",
        "fixture-prototype-v1",
        policy.policy_version,
        "cal-v1",
        FINGERPRINT,
        "neocortex.synthetic-fast-curation",
    )


def test_bundle_roundtrip_is_pure_and_requires_exact_scope() -> None:
    bundle = _bundle()
    loaded = load_calibrated_policy(bundle.as_dict())
    config = FastCurationConfig.from_policy_bundle(loaded)
    assert config.model.model_signature == loaded.model_signature
    assert loaded.prototype_set_fingerprint == FINGERPRINT
    assert loaded.prototype_scope == "neocortex.synthetic-fast-curation"
    assert loaded.required_families == ()
    assert loaded.max_evidence == 5
    packaged = default_calibrated_policy()
    assert packaged is not None
    assert packaged.prototype_set_fingerprint == "91fe4911163b0ce0c66437259109bdf3879638d2be9bfb682d931f095e379de2"
    assert default_prototypes().fingerprint == packaged.prototype_set_fingerprint
    assert default_prototypes().ontology_id == packaged.prototype_scope

    current = {
        "model_signature": bundle.model_signature,
        "representation_version": "fast-curation-document-representation-v1",
        "ontology_version": "fixture-v1",
        "prototype_version": "fixture-prototype-v1",
        "prototype_set_fingerprint": FINGERPRINT,
        "policy_version": bundle.policy_version,
        "calibration_version": "cal-v1",
        "input_signature": "input-v1",
    }
    assert validate_record_versions( current, loaded, source_input_signature="input-v1").valid
    assert not validate_record_versions(
        {**current, "model_signature": "old-model"}, loaded
    ).valid
    assert validate_record_versions(
        {**current, "input_signature": "old-input"},
        loaded,
        source_input_signature="input-v1",
    ).reason == "stale_input_signature"


def test_bundle_rejects_unmeasured_or_wrong_prototype_scope() -> None:
    calibration = CalibrationParameters(
        "cal-v1", "model-v1", False, {"document_kind": 0.7}, {"document_kind": 0.1}
    )
    with pytest.raises(ValueError, match="measured"):
        FastCurationPolicyBundle(
            FastCurationPolicy.from_calibration(calibration),
            "model-v1",
            "rep",
            "ontology",
            "proto",
            "fast-curation-policy-v1",
            "cal-v1",
            FINGERPRINT,
            "scope",
        )
    current = {
        "model_signature": _bundle().model_signature,
        "representation_version": "fast-curation-document-representation-v1",
        "ontology_version": "fixture-v1",
        "prototype_version": "fixture-prototype-v1",
        "prototype_set_fingerprint": "b" * 64,
        "policy_version": "fast-curation-policy-v1",
        "calibration_version": "cal-v1",
    }
    assert validate_record_versions(current, _bundle()).reason == "stale_prototype_set_fingerprint"
