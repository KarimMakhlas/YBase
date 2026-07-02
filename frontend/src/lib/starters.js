// Starter questions for the empty Chat state and the Home ask bar.
// Shared so both surfaces offer the same, workspace-aware prompts.

// Generic fallbacks — used before stats load, or when a workspace has no
// decisions yet to derive concrete questions from. Workspace-agnostic on
// purpose (the old hard-coded Postgres/Mongo prompts only fit the demo corpus).
export const STARTERS = [
  'What are our most recent decisions, and why?',
  'What important questions are still open?',
  'What did we change our minds about recently?',
  'Summarize the key decisions in memory.',
]

// Turn the workspace's actual memory into concrete starter questions, so a
// populated workspace is offered prompts it can really answer.
export function startersFromStats(stats) {
  const clip = (s) => (s && s.length > 60 ? `${s.slice(0, 59)}…` : s)
  const out = []
  for (const d of (stats?.recent_decisions || []).slice(0, 2)) {
    if (d.title) out.push(`Why did we decide “${clip(d.title)}”?`)
  }
  for (const q of (stats?.open_questions || []).slice(0, 1)) {
    if (q.title) out.push(`What’s the latest on “${clip(q.title)}”?`)
  }
  return out
}

// Merge derived prompts with generic fallbacks, capped at `n`.
