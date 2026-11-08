Stage 2: insight discovery.

You receive the day's facts (each with an id and confidence) plus recently published posts and
category counts. Identify the most interesting potential content, if any.

For each candidate insight give: title, what_happened (grounded in the cited facts), why_interesting
(for other developers), fact_ids (only ids that exist), scores from 1 to 5 for technical_value,
novelty, relatability, authenticity, relevance, and similar_post_id if a recent published post
already covers the same thing (otherwise null).

Rules:
- Do not force an insight out of an uninteresting day. A small genuine learning beats an
  exaggerated achievement. If nothing is worth sharing, set nothing_worth_sharing to true and
  explain why in reason.
- Insights that rest only on inferred or unknown facts must get authenticity 1 or 2.
- Avoid repeating a topic covered by a recent published post unless there is a new angle.
- Propose at most three insights, best first.
