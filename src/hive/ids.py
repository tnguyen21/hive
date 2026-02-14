"""Hash-based ID generation for human-readable, collision-free identifiers."""

import uuid


def generate_id(prefix: str = "w", length: int = 12) -> str:
    """
    Generate a hash-based ID with a prefix.

    Args:
        prefix: String prefix for the ID (e.g., "w" for work, "agent" for agents).
               If empty, no prefix or separator is added.
        length: Number of hex characters to use for the ID suffix. Default is 12.

    Returns:
        ID string in format "{prefix}-{hash[:length]}" (e.g., "w-a3f8b1c4d2e5").
        If prefix is empty, returns just the hash suffix.

    Example:
        >>> id1 = generate_id("w")
        >>> id1.startswith("w-")
        True
        >>> len(id1)
        14
    """
    unique_part = uuid.uuid4().hex[:length]
    if prefix:
        return f"{prefix}-{unique_part}"
    return unique_part
