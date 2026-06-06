"""Summarize task — summarize text, documents, or code."""

from __future__ import annotations


def summarize(content: str, format: str = "bullets") -> dict:
    """Build a task spec for summarizing content."""
    fmt_instruction = {
        "bullets": "Use concise bullet points.",
        "paragraph": "Write a coherent paragraph summary.",
        "tldr": "Write a single sentence TL;DR.",
    }.get(format, f"Use this format: {format}")

    return {
        "name": "summarize",
        "kind": "llm",
        "prompt": (
            f"Summarize the following content:\n\n{content}\n\n"
            f"{fmt_instruction}\n"
            "Focus on the most important points. Be concise."
        ),
        "preferred_providers": ["claude-haiku"],
    }
