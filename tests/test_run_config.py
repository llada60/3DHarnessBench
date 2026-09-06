import json
from pathlib import Path

from raw_agents.run_config import atomic_write_json


def test_atomic_write_json_replaces_content_and_leaves_no_temp_file(
    tmp_path: Path,
):
    checkpoint = tmp_path / "nested" / "checkpoint.json"
    atomic_write_json(checkpoint, {"attempt": 1})
    atomic_write_json(checkpoint, {"attempt": 2, "status": "resumable"})

    assert json.loads(checkpoint.read_text()) == {
        "attempt": 2,
        "status": "resumable",
    }
    assert list(checkpoint.parent.glob("*.tmp")) == []
