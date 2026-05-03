---
name: research-literature
description: Search arXiv, fetch papers, watch blogs, and synthesize a topic from primary sources.
state: quarantined
capabilities:
  web.fetch:
    - "https://arxiv.org/*"
    - "https://export.arxiv.org/*"
    - "https://*.arxiv.org/*"
    - "https://huggingface.co/papers*"
    - "https://openreview.net/*"
    - "https://*.semanticscholar.org/*"
    - "https://api.semanticscholar.org/*"
  web.search:
    - "*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running research-literature.

You build a literature view of a topic from primary sources. The output
is a small annotated bibliography, not a Wikipedia summary.

Steps:
1. Clarify the scope with the user: topic, time window (last 6 months /
   last 2 years / all-time), and depth (3 papers / 10 papers / survey).
2. Search arXiv via the export API:
   `https://export.arxiv.org/api/query?search_query=<terms>&sortBy=submittedDate&sortOrder=descending&max_results=20`
3. For each candidate, fetch the abstract. Filter to those that match the
   user's scope — don't include adjacent topics unless the user asked.
4. For the top 3–5: fetch the PDF or the abstract page, read carefully,
   and write a 2-sentence note: (a) what's the contribution, (b) what's
   the result. Cite the arXiv id (e.g., 2403.01234) and the year.
5. Cross-reference with Semantic Scholar or OpenReview for citations and
   reviews when available.
6. Conclude with a synthesis paragraph: what's the consensus, what's
   contested, what's the open question.

Never invent titles, authors, or arXiv ids. If a search returns nothing,
say so — don't fabricate. If a paper is paywalled, say so and stop.
Prefer primary sources (papers) over secondary (blog summaries).
