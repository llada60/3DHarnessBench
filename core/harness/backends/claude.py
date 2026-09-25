"""Claude Code backend shared by the Opus and Fable models."""

from core.harness import no_memory

from .common import (
    _json_safe,
)
from .common import (
    extract_script as extract_script,
)
from .common import (
    is_backend_limit_error as is_backend_limit_error,
)
from .common import (
    load_image as load_image,
)
from .common import (
    render_script as render_script,
)
from .common import (
    rendered_views as rendered_views,
)

BACKEND = "claude-code"

MEMORY_OFF = no_memory.apply_claude


def build_agent(vision_model, credentials):
    """Build the bundled TaskSolver Claude Code interface.

    The small wrapper preserves the former ``Agent.visual_interface`` shape
    without importing TaskSolver's unrelated provider registry.
    """
    from types import SimpleNamespace

    from tasksolver.answer_types import TextAnswer
    from tasksolver.claude_code import ClaudeCodeModel
    from tasksolver.common import TaskSpec

    task = TaskSpec(
        name="",
        description="",
        answer_type=TextAnswer,
        followup_func=None,
        completed_func=None,
    )
    aliases = {
        "claude-code-fable-5": "claude-fable-5",
        "claude-code-opus-5": "claude-opus-5",
    }
    if vision_model in aliases:
        model = aliases[vision_model]
    else:
        raise ValueError(f"Unsupported Claude Code model alias: {vision_model}")
    return SimpleNamespace(visual_interface=ClaudeCodeModel(None, task, model=model))


def call_model(interface, text, pil_images, max_tokens, model):
    """One backend turn: a Question of (text + inline images) via rough_guess
    (which skips first_question, so `text` is delivered verbatim). Returns
    (reply_text, raw_dict)."""
    from tasksolver.common import Question

    question = Question([text] + list(pil_images))
    p_ans, response, meta, payload = interface.rough_guess(
        question, max_tokens=max_tokens, max_tries=1
    )
    reply = getattr(p_ans, "data", None) or getattr(p_ans, "raw", None) or ""
    raw = {
        "backend": BACKEND,
        "model": model,
        "thinking_depth": interface.thinking_depth,
        "text": reply,
        "n_images": len(pil_images),
        "response_metadata": _json_safe(meta),
    }
    if not reply:
        raise RuntimeError(
            f"{BACKEND} returned no output for model={model}. Ensure the "
            "backend is authenticated/reachable."
        )
    return reply, raw
