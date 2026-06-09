"""Fan-out / merge: dispatch N agent tasks in parallel, await all, summarize.

The most common multi-agent pattern: parallel specialists + a synthesis
step. Each child run gets its own aitelier_run_id; they share a
`trace_tag` so a later `/v1/traces?trace_tag=...` rolls them up.

Run with `uv run python 01_fanout_merge.py` against a running aitelier
(`make start` from the repo root).
"""

from __future__ import annotations

import asyncio
import uuid

from aitelier_client import Aitelier


async def fanout_and_summarize() -> str:
    ait = Aitelier(base_url="http://localhost:7777")
    openai = await ait.openai()

    # 1. Choose specialists. Each gets its own agent backend or model
    #    routing string. Mix and match LiteLLM models + agent backends.
    specs = [
        ("security audit",   "agent:claude"),
        ("dependency audit", "agent:codex"),
        ("docstring audit",  "claude-haiku"),
    ]
    workflow_tag = f"fanout-{uuid.uuid4().hex[:8]}"

    # 2. Submit children in parallel. Each one is async — we get a
    #    run_id immediately and `wait_for_run` blocks until terminal.
    async def submit_and_wait(role: str, model: str) -> str:
        if model.startswith("agent:"):
            submission = await ait.submit_run(
                model=model,
                messages=[
                    {"role": "system", "content": f"You are doing {role}."},
                    {"role": "user", "content": "Reply with one sentence."},
                ],
                aitelier_opts={"trace_tag": workflow_tag},
            )
            run = await ait.wait_for_run(submission["run_id"], timeout=60)
            return run.result.get("content", "(empty)")

        # LLM path: sync chat completion.
        resp = await openai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": f"You are doing {role}."},
                {"role": "user", "content": "Reply with one sentence."},
            ],
        )
        return resp.choices[0].message.content or "(empty)"

    parts = await asyncio.gather(*[
        submit_and_wait(role, model) for role, model in specs
    ])

    # 3. Synthesize. A single LLM call merges the parallel outputs.
    merged_input = "\n\n".join(
        f"## {role}\n{out}" for (role, _), out in zip(specs, parts)
    )
    final = await openai.chat.completions.create(
        model="claude-haiku",
        messages=[
            {"role": "system",
             "content": "Summarize the three audits into one paragraph."},
            {"role": "user", "content": merged_input},
        ],
    )
    return final.choices[0].message.content or "(empty summary)"


if __name__ == "__main__":
    print(asyncio.run(fanout_and_summarize()))
