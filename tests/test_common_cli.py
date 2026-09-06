import argparse

import pytest

from raw_agents.agent_pools import output_pool_alias
from raw_agents.common_cli import boolean_value, positive_int


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", True])
def test_boolean_value_accepts_true_values(value):
    assert boolean_value(value) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", False])
def test_boolean_value_accepts_false_values(value):
    assert boolean_value(value) is False


def test_boolean_value_rejects_unknown_value():
    with pytest.raises(argparse.ArgumentTypeError, match="expected true/false"):
        boolean_value("sometimes")


@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_positive_int_rejects_non_positive_or_non_integer(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)


def test_public_agent_alias_has_stable_output_pool():
    assert output_pool_alias("qwen3-8-max-preview") == "qwen3-8-max-preview"
    assert output_pool_alias("gpt-5-6-sol") == "gpt-5-6-sol"
