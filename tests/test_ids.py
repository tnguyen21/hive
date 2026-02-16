"""Tests for ID generation."""

import re

from hive.utils import generate_id


def test_generate_id_format():
    """Test that generated IDs follow the expected format."""
    # Default is now 12 chars
    issue_id = generate_id("w")
    assert re.match(r"^w-[a-f0-9]{12}$", issue_id)

    agent_id = generate_id("agent")
    assert re.match(r"^agent-[a-f0-9]{12}$", agent_id)


def test_generate_id_uniqueness():
    """Test that generated IDs are unique."""
    ids = [generate_id("w") for _ in range(1000)]
    assert len(ids) == len(set(ids)), "Generated IDs should be unique"


def test_generate_id_default_prefix():
    """Test default prefix is 'w'."""
    issue_id = generate_id()
    assert issue_id.startswith("w-")


def test_generate_id_custom_prefix():
    """Test custom prefixes work correctly."""
    prefixes = ["task", "bug", "feature", "agent"]
    for prefix in prefixes:
        id_val = generate_id(prefix)
        assert id_val.startswith(f"{prefix}-")
        assert len(id_val) == len(prefix) + 13  # prefix + "-" + 12 hex chars


def test_generate_id_empty_prefix():
    """Test empty prefix returns just the hash."""
    id_val = generate_id("")
    assert re.match(r"^[a-f0-9]{12}$", id_val)
    assert "-" not in id_val


def test_generate_id_custom_length():
    """Test custom length."""
    id_val = generate_id("w", length=8)
    assert len(id_val) == 2 + 8  # "w-" + 8 chars
    assert re.match(r"^w-[a-f0-9]{8}$", id_val)

    id_val = generate_id("", length=16)
    assert len(id_val) == 16
    assert re.match(r"^[a-f0-9]{16}$", id_val)
