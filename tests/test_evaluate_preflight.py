from pathlib import Path

import pytest

from evaluation.evaluate import validate_uni3d_repo


def test_validate_uni3d_repo_accepts_required_module(tmp_path: Path):
    repo = tmp_path / "Uni3D"
    module = repo / "models" / "point_encoder.py"
    module.parent.mkdir(parents=True)
    module.write_text("# test fixture\n")

    assert validate_uni3d_repo(repo) == repo.resolve()


def test_validate_uni3d_repo_reports_actionable_error(tmp_path: Path):
    repo = tmp_path / "missing-Uni3D"

    with pytest.raises(SystemExit) as error:
        validate_uni3d_repo(repo)

    message = str(error.value)
    assert str(repo / "models" / "point_encoder.py") in message
    assert "UNI3D_REPO" in message
    assert "does not enforce a Uni3D commit or tag" in message
