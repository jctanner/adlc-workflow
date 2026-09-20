from pathlib import Path

import pytest

from adlc_workflow.artifacts import ArtifactLayout, ArtifactLayoutError
from adlc_workflow.profiles import load_profile


INSTALL = Path(__file__).parents[2]
PROFILE = "config/rhai-feature-creator.yaml"


def test_result_file_is_the_authoritative_result_path(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    profile["artifacts"]["result_file"] = "canonical/result.json"
    profile["artifacts"]["evidence_files"]["result"] = "stale-result.json"

    layout = ArtifactLayout(tmp_path, profile)

    assert layout.evidence_path("result") == tmp_path / "artifacts" / "canonical/result.json"


def test_result_file_is_required(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    profile["artifacts"].pop("result_file")

    with pytest.raises(ArtifactLayoutError, match="result_file"):
        ArtifactLayout(tmp_path, profile)
