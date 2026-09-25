"""Shared generation, editing, and repair prompts for both image settings.

Image attachment order and counts are supplied by the iterative runner. Keep
prompt wording here so prompt changes can be reviewed separately from execution.
"""


def initial_prompt(base_prompt: str, n_target: int) -> str:
    return (
        f"{n_target} GT reference image(s) of one target object are attached. "
        "Inspect all of them before writing the reconstruction.\n\n"
        f"{base_prompt}"
    )


def editing_prompt(previous_script: str, n_current: int, n_target: int) -> str:
    return f"""The attached images are ordered as follows: first {n_current} image(s)
are renders of the CURRENT generated script, followed by {n_target} GT reference
image(s) of the TARGET object. They depict the current reconstruction and the
desired object respectively.

Here is the complete current Blender script:
```python
{previous_script}
```

Compare the current renders against every GT view. Identify the largest visible
geometry, proportion, placement, silhouette, or missing-part errors, then edit
the script to correct them. Preserve useful detail already present. Return the
COMPLETE corrected self-contained Blender Python script, not a diff. Do not use
ellipsis and do not omit unchanged code. Output no prose outside the script.
"""


def render_feedback(base_prompt: str, failed_script: str, record: dict) -> str:
    error = (record.get("error") or "(no Blender error was recorded)").strip()
    return f"""{base_prompt}

--- PREVIOUS SCRIPT FAILED TO RENDER ---
The previous script failed in Blender with status {record.get('status')}.

Failed script:
```python
{failed_script}
```

Blender error:
{error}

Fix the failure and return the COMPLETE corrected script. Output no prose
outside the script.
"""


def parse_feedback(base_prompt: str, reply: str) -> str:
    return f"""{base_prompt}

--- PREVIOUS REPLY WAS NOT A PARSABLE PYTHON SCRIPT ---
The previous reply could not be extracted as Python. Return one complete,
self-contained Blender Python script and no prose. Do not abbreviate it.

Previous reply:
{reply[-4000:]}
"""
