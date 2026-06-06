"""Research task — investigate a topic and produce a structured summary."""

from __future__ import annotations


def research(topic: str, depth: str = "standard") -> dict:
    """Build a task spec for researching a topic."""
    return {
        "name": "research",
        "kind": "llm",
        "prompt": (
            f"Research the following topic in depth: {topic}\n\n"
            "Produce a structured summary with:\n"
            "1. Key concepts and definitions\n"
            "2. Current state of the art\n"
            "3. Trade-offs and alternatives\n"
            "4. Practical recommendations\n"
            "5. Sources and references where applicable\n\n"
            f"Depth level: {depth}"
        ),
        "preferred_providers": ["claude-sonnet"],
    }
