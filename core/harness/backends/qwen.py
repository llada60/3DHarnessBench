"""Qwen Coding Plan backend for iterative image runs."""

import os

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

VISION_MODEL = "qwen3"  # tasksolver generic vLLM path (honours QWEN3_* env)

BACKEND = "tokenplan"

API_KEY_ENV = "QWEN_API_KEY"


def _resolve_api_key(credentials):
    """Explicit credentials, then exported variables / the checkout's .env."""
    from core.harness.project_env import load_project_env

    if credentials:
        return credentials
    load_project_env()
    return os.environ.get(API_KEY_ENV, "")


def build_agent(vision_model, credentials):
    """Build the bundled TaskSolver vLLM interface directly."""
    from types import SimpleNamespace

    from tasksolver.answer_types import TextAnswer
    from tasksolver.common import TaskSpec
    from tasksolver.vllm import (
        VLLMModel,
        resolve_qwen3_base_url,
        resolve_qwen3_model_name,
    )

    class BailianVLLMModel(VLLMModel):
        """The token-plan endpoint speaks OpenAI-compatible chat completions
        but does not accept vLLM's `chat_template_kwargs` extra body; its
        Qwen3 thinking models take `enable_thinking` as a top-level request
        field instead (accepted with or without streaming)."""

        def _default_extra_body(self) -> dict:
            return {"enable_thinking": self.thinking_depth is not None}

    task = TaskSpec(
        name="",
        description="",
        answer_type=TextAnswer,
        followup_func=None,
        completed_func=None,
    )
    if vision_model != "qwen3":
        raise ValueError(f"Unsupported bundled vLLM backend: {vision_model}")
    api_key = _resolve_api_key(credentials)
    base_url = resolve_qwen3_base_url()
    model = resolve_qwen3_model_name(base_url)
    if not api_key or not base_url:
        raise ValueError(
            f"qwen3 requires {API_KEY_ENV} and an OpenAI-compatible base URL"
        )
    return SimpleNamespace(
        visual_interface=BailianVLLMModel(
            api_key=api_key, task=task, model=model, base_url=base_url
        )
    )


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
