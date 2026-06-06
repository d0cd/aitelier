/**
 * SSE streaming utilities for the aitelier TypeScript SDK.
 */

import type { AtelierEvent } from "./_generated/types.js";

/**
 * Process a stream of SSE events, yielding typed events.
 * Terminates on run.completed or run.error.
 */
export async function* streamEvents(
  eventIter: AsyncIterable<{ type: string; data: Record<string, unknown> }>
): AsyncIterable<AtelierEvent> {
  for await (const raw of eventIter) {
    const event: AtelierEvent = {
      type: raw.type as AtelierEvent["type"],
      timestamp: (raw.data.timestamp as string) ?? new Date().toISOString(),
      ...(raw.data.run_id != null && { runId: raw.data.run_id as string }),
      ...(raw.data.provider != null && {
        provider: raw.data.provider as string,
      }),
      data: raw.data,
    };

    yield event;

    if (raw.type === "run.completed" || raw.type === "run.error") {
      return;
    }
  }
}
