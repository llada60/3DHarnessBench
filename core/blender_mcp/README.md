# Blender MCP dependencies

ActiveVisual and Full3DInteraction share `source.py` for source selection and
`runtime.py` for process configuration. Task runners do not edit MCP source or
its package metadata.

## Source selection

The `[official]` section in each task configuration selects Blender's MCP
repository. The default is upstream tag `v1.0.0`, verified against commit
`03004fd0216bfe5e0a3d9ac9b47d5efadc3d78c4`.

Remote checkouts are cached by repository URL and commit. A moved tag, wrong
revision, or modified cache fails explicitly. Downloads are staged and published
atomically under a per-checkout lock. Old caches named `blender_mcp-v1.0.0`
are not reused because previous releases modified their contents at startup.

For development, set this option in the existing `[official]` section:

```toml
source_path = "/path/to/blender_mcp"
```

Relative paths resolve from the benchmark repository root. A local checkout must
contain both `addon/blender_mcp_addon` and `mcp/blmcp`; it is used as-is and takes
precedence over all remote source settings. For a published fork, set
`source_url`, `source_ref`, and `source_commit` to that fork's URL, tag or branch,
and full commit SHA. Keep compatibility changes and their tests in that repository.

## Runtime dependencies

`requirements.txt` pins the harness's MCP SDK to `1.29.0`, preserving the previous
SDK selection. `uv run --no-project --with <source>/mcp --with-requirements ...`
resolves that constraint together with the selected package in a uv-managed
environment, with the repository root selected via `--directory` so the runtime
requirements file can use a relative path. It does not rewrite the upstream `pyproject.toml` or generate an
upstream `uv.lock`. Transitive dependency versions are not fully locked.

## Screenshot compatibility

Previous releases replaced the upstream area screenshot implementation with
GPUOffScreen rendering at startup, motivated by a reported black framebuffer
under Xvfb. That implicit patch has been removed. Official checkouts now use the
upstream screenshot operator. This change has not been validated with a live
Blender/Xvfb session; deployments that encounter black screenshots need a
reproduced fix in the MCP repository and an explicitly configured fork.

The restricted reference server in `viewport_only/` is a separate bundled
implementation with its own screenshot code and license.

## Blender startup

Blender imports the task bootstrap modules through `--python-expr`. The
ActiveVisual bootstrap imports its shared helpers and the bundled viewport
add-on normally. Custom add-ons use an importable module name (`--addon-module` or
`VIEWPORT_ONLY_ADDON_MODULE`); install the package or put it on `PYTHONPATH`.

Task runners launch shell scripts with repository-relative paths and an explicit
working directory. The public entry points use package imports and retain the
cache location from the task configuration.

## Validation

Run the source selection tests without Blender or network access:

```bash
python3 -m unittest discover -s tests -p 'test_blender_mcp_source.py' -v
```
