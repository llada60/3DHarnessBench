from pathlib import Path

import pytest

from raw_agents.mcp_runner import discover_instances


def add_instance(root: Path, name: str, *, complete: bool = True) -> None:
    directory = root / name
    directory.mkdir()
    (directory / f"{name}.glb").touch()
    if complete:
        (directory / f"{name}_grey.glb").touch()


def test_discover_instances_is_sorted_and_ignores_incomplete_entries(
    tmp_path: Path,
):
    add_instance(tmp_path, "ZebraFactory")
    add_instance(tmp_path, "AppleFactory")
    add_instance(tmp_path, "IncompleteFactory", complete=False)

    assert discover_instances(tmp_path) == ["AppleFactory", "ZebraFactory"]


def test_discover_instances_validates_requested_names(tmp_path: Path):
    add_instance(tmp_path, "VaseFactory")

    with pytest.raises(RuntimeError, match="unknown benchmark instance"):
        discover_instances(tmp_path, requested=["MissingFactory"])


def test_discover_instances_applies_limit_after_sorting(tmp_path: Path):
    add_instance(tmp_path, "SecondFactory")
    add_instance(tmp_path, "FirstFactory")

    assert discover_instances(tmp_path, limit=1) == ["FirstFactory"]
