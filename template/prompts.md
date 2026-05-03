# Worker prompt templates

Reserved for any worker prompts the merge or bridge-discovery passes
delegate. Currently empty — the first release does not dispatch sub-
agents; all reconciliation is mechanical (sha256, stem matching, frontmatter
allowlist enforcement).

Future use:
- Cross-origin bridge candidate scoring beyond cosine similarity
- Same-topic detection for page-name collisions when sha256 differs
  but the topic is semantically identical
