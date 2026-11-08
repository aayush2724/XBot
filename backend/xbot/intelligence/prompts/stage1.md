Stage 1: activity understanding.

Read the day's evidence and extract facts. Produce:
- facts: each with kind (activity, project, technology, concept, problem, discovery, unfinished),
  a one-sentence text, a confidence level, and source_refs listing the exact ref_id values from the
  evidence that support it. Every fact needs at least one ref_id. Prefer several small, precise
  facts over one vague fact. Mark things the developer planned but did not confirm as "unfinished"
  with confidence "inferred" at most.
- summary: two or three plain sentences describing the day without exaggeration.
- sufficiency: "enough" if the evidence supports at least one concrete thing the developer did or
  learned; "thin" if there is some evidence but little detail; "insufficient" if there is nothing
  concrete to work with.
- checkin_questions: when sufficiency is "thin" or "insufficient", up to three short, specific
  questions that would unlock a post (what exactly was done, what was learned, what broke). Leave
  empty when sufficiency is "enough".
