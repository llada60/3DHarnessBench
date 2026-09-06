from cli_agents import checkpoint


def resumable_state(**overrides):
    state = {
        "version": checkpoint.VERSION,
        "state": checkpoint.RESUMABLE,
        "session": {
            "kind": "codex",
            "model": "gpt-5.6-sol",
            "resume_target": "session-id",
        },
        "inputs_digest": "same-inputs",
        "attempts": 1,
    }
    state.update(overrides)
    return state


def test_checkpoint_is_usable_for_matching_resume():
    usable, reason = checkpoint.usable(
        resumable_state(),
        kind="codex",
        model="gpt-5.6-sol",
        digest="same-inputs",
        max_attempts=5,
    )

    assert usable is True
    assert reason == "resuming attempt 2"


def test_checkpoint_rejects_changed_inputs():
    usable, reason = checkpoint.usable(
        resumable_state(),
        kind="codex",
        model="gpt-5.6-sol",
        digest="different-inputs",
        max_attempts=5,
    )

    assert usable is False
    assert "inputs changed" in reason


def test_checkpoint_rejects_exhausted_attempt_budget():
    usable, reason = checkpoint.usable(
        resumable_state(attempts=5),
        kind="codex",
        model="gpt-5.6-sol",
        digest="same-inputs",
        max_attempts=5,
    )

    assert usable is False
    assert "max_attempts limit" in reason
