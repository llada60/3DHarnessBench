# Contributing to 3DHarnessBench

Thank you for helping improve 3DHarnessBench. We welcome bug reports and pull
requests. Maintainers review submissions manually; opening an issue before a
large change is encouraged so effort is not duplicated.

## Report a bug

Use the bug report template and include:

- the command you ran and the complete error message;
- operating system, GPU, driver, Blender, Pixi and Python versions;
- the harness setting and agent name;
- the smallest task or input that reproduces the problem; and
- whether the problem occurs with a fresh clone and `pixi install --locked`.

Remove API keys, access tokens, private model outputs and other credentials
from logs before posting them.

## Submit a pull request

1. Fork the repository and create a focused branch.
2. Keep generated outputs, downloaded data, weights, caches and `.env` files
   out of the commit.
3. Add or update tests for behavior changes.
4. Run the local checks below.
5. Describe the change, its motivation and the validation performed in the PR.

Small, reviewable pull requests are preferred. Do not reformat unrelated
files or change benchmark defaults without explaining the compatibility
impact.

## Development checks

The lightweight CI environment is independent from the full GPU environment:

```bash
pixi install --manifest-path .github/ci/pixi.toml --locked
pixi run --manifest-path .github/ci/pixi.toml check
```

The checks run pytest, critical-error Ruff rules, ShellCheck, and `--help`
smoke tests. They do not install or launch Blender, use a GPU, contact model
providers, or download evaluation weights.

For runtime changes, also follow the relevant fresh-clone steps in
[INSTALL.md](INSTALL.md). Maintainers perform the Linux/NVIDIA end-to-end
check before a release tag is created.

Dependency and GitHub Action updates are reviewed manually. Keep lockfiles in
the same pull request as their manifests and explain any changed pins.

## Contribution licensing

By submitting a contribution, you agree that it is licensed under the same
terms as the material it modifies:

- software and software configuration: GPL-3.0-only;
- documentation, standalone prompt templates, skills, project images and
  benchmark content: CC BY 4.0; and
- files under `BlenderMCP/` that retain an MIT notice: MIT.

See [LICENSING.md](LICENSING.md) for the exact boundaries. No contributor
license agreement or Developer Certificate of Origin sign-off is required.
