import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metrics import evaluate


class EvaluationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = self.root / "results"
        self.grey_results = self.root / "grey"
        self.agent = "gpt-5-6-sol"
        self.model = self.results / self.agent
        self.grey_model = self.grey_results / self.agent
        self.names = ["First", "Second"]
        self.args = evaluate.parse_args([
            "--data-path", str(self.root / "gt"),
            "--agent", self.agent, "--setting", "Single-view",
            "--output-dir", str(self.root / "outputs"), "--grey-shaded",
        ])
        for name in self.names:
            script = self.model / name / f"{name}.py"
            script.parent.mkdir(parents=True)
            script.write_text("# original script\n")
        self.calls = []

    def worker(self, label, command, dry_run):
        self.calls.append((label, command))
        names = command[command.index("--instances") + 1:]
        names = names[:next((i for i, name in enumerate(names)
                             if name.startswith("--")), len(names))]
        if not dry_run:
            if label == "prepare:glb":
                root = self.model / "_evaluation_artifacts"
                for name in names:
                    output = root / name / f"{name}.glb"
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"glb")
                (root / "preprocess_summary.json").write_text(json.dumps({
                    "geometry_prep_version": evaluate.GEOMETRY_PREP_VERSION,
                }))
            else:
                grey = label == "prepare:grey-renders"
                for name in names:
                    folder = ((self.grey_model if grey else self.model)
                              / name / ("grey_renders" if grey else "renders"))
                    folder.mkdir(parents=True, exist_ok=True)
                    for view in evaluate.VIEWS:
                        (folder / view).touch()
                    (folder / "render_log.json").write_text(json.dumps({
                        "status": "OK", "rig_version": evaluate.SCENE_RIG_VERSION,
                        "geometry_prep_version": evaluate.GEOMETRY_PREP_VERSION,
                        "light_power": 0.5 if grey else 1.0,
                        "light_ref_extent": self.args.light_ref_extent,
                        "grey_shaded": grey, "grey_value": evaluate.GREY_MATERIAL_VALUE,
                    }))
        return {"label": label, "command": command,
                "status": "DRY_RUN" if dry_run else "OK", "returncode": None if dry_run else 0}

    def prepare(self, worker=None):
        with patch.object(evaluate, "run_command", side_effect=worker or self.worker):
            with contextlib.redirect_stdout(io.StringIO()):
                evaluate.prepare_predictions(
                    self.args, self.model, self.results, self.grey_results,
                    self.grey_model, self.agent, self.names, [])

    def test_refresh_only_changed_script_even_with_preserved_mtime(self):
        self.prepare()
        self.assertEqual(len(self.calls), 3)
        self.calls.clear()
        self.prepare()
        self.assertEqual(self.calls, [])
        script = self.model / "First" / "First.py"
        original = script.stat()
        script.write_text("# modified script\n")
        os.utime(script, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.prepare()
        self.assertEqual(len(self.calls), 3)
        for label, command in self.calls:
            self.assertIn("--overwrite", command)
            tail = command[command.index("--instances") + 1:]
            self.assertEqual(tail[0], "First")
            self.assertNotIn("Second", tail)

    def test_failed_preparation_does_not_accept_changed_script(self):
        self.prepare()
        script = self.model / "First" / "First.py"
        script.write_text("# changed\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*sha256")}
        self.prepare(lambda label, command, dry_run: {"status": "FAILED"})
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.calls.clear()
        self.prepare()
        self.assertEqual(len(self.calls), 3)

    def test_legacy_artifacts_without_fingerprints_are_rebuilt(self):
        self.prepare()
        for path in self.root.rglob("*sha256"):
            path.unlink()
        self.calls.clear()
        self.prepare()
        self.assertEqual(len(self.calls), 3)

    def test_grey_renders_refresh_when_enabled_after_script_change(self):
        self.prepare()
        (self.model / "First" / "First.py").write_text("# changed\n")
        self.args.grey_shaded = False
        self.calls.clear()
        self.prepare()
        self.assertEqual(len(self.calls), 2)
        self.args.grey_shaded = True
        self.calls.clear()
        self.prepare()
        self.assertEqual([label for label, _ in self.calls], ["prepare:grey-renders"])

    def test_dry_run_does_not_write_artifacts_or_fingerprints(self):
        self.args.dry_run = True
        before = set(self.root.rglob("*"))
        self.prepare()
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertEqual(len(self.calls), 3)

    def test_existing_metrics_are_always_run_when_preparation_is_skipped(self):
        outputs = self.args.output_dir
        model = outputs / "Single-view" / "w_texture" / self.agent
        gt = self.args.data_path
        for name in self.names:
            (model / name).mkdir(parents=True)
            (model / name / f"{name}.py").write_text("# generated\n")
            (gt / name / "color_renders").mkdir(parents=True)
            (gt / name / f"{name}.glb").touch()
        metrics = model / "_metrics"
        metrics.mkdir()
        for filename in evaluate.FINAL_METRIC_FILES.values():
            (metrics / filename).write_text("{}")
        argv = ["--data-path", str(gt), "--agent", self.agent,
                "--setting", "Single-view", "--output-dir", str(outputs),
                "--skip-prepare", "--dry-run"]
        with patch.object(evaluate, "validate_uni3d_repo", return_value=self.root):
            with patch.object(evaluate, "run_command", side_effect=self.worker):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(evaluate.main(argv), 0)
        self.assertEqual(len(self.calls), 7)
        self.assertTrue(all(label.startswith("metric:") for label, _ in self.calls))

    def test_removed_flags_are_rejected(self):
        base = ["--data-path", str(self.root), "--agent", self.agent,
                "--setting", "Single-view"]
        for flag in ("--overwrite-metrics", "--overwrite-artifacts"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    evaluate.parse_args([*base, flag])
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
