# Explicit context-v2 operationalization supplement

This supplement implements the authorized plan's literal requirement: do not
present unsupported material as **sufficient evidence**. The original v1
harness applied a stricter, additional rule: every selected context item for a
negative query was counted as unsupported, including merely related material.
That difference was reported before changing the harness, and the root agent
explicitly directed a separate versioned operationalization before any
candidate SHA freeze or reserved gate.

Nothing here changes `freeze.json`, source bytes, queries, grades, partitions or
reserved data. The baseline is immutable and is not retroactively relabeled.
In particular, the original DEV baseline retains eight negative context
selections across three of its four negative queries. A typed-v2 zero must
never be described as "zero legacy false positives."

The v1 implementation SHA-256 is
`cce9ebcd0dbd37a34fb31cb9bf36b2307e3b6acad8e3a58220664b7e923ed0e8`,
the unchanged dataset-freeze SHA-256 is
`02c43c7100db4493785b3bd69ae43358115e800050eac8ac1d0fff4817921ce9`,
and the original canonical DEV baseline SHA-256 is
`d793c199718b68bc4a3701cc86a88f0605af54e1a7f17197175539fe5cfe931f`.
`operationalization-v2.json` records the additional implementation hash and
the explicit decision. The driver checks that supplement before executing a
candidate with `--context-response-version 2`.

## Separate measurements, not a rewritten denominator

The candidate driver captures both the public legacy-v1 context and the public
v2 context, in addition to the unchanged resource-level search measurement.
`negative_unsupported_evidence` and `legacy_negative_context_selections` keep
the v1 method visible. The v2 result also preserves the raw number of negative
citations under that same selection-counting method, and separately reports
`unsupported_sufficient_evidence`, verified related material and unverifiable
dispositions. Baseline and candidate must identify which response surface was
counted; a v2 `citations` array cannot silently become an empty v1
`selected_hits` array.

For a negative query, `evidence_candidate` always counts as unsupported
sufficient evidence. A missing, malformed, unknown or unverifiable disposition
also fails the gate, rather than being excluded. `related_only` is excluded
from that sufficient-evidence count only when all of these checks hold:

- The citation resolves to exactly one frozen source and to the captured
  retrieval's resource, revision and evidence identifiers
- Its current source bytes match the frozen SHA-256, its excerpt exists in
  those bytes or their authored text representation, and its source locator
  and final emitted-text range are explicit and structurally valid
- Necessary-witness requirements are a recognized policy family applicable to
  the actual query, the missing requirements are nonempty, and independently
  repeated literal checks agree with the final excerpt
- `evaluated_chars`, final-scope labels, query bounds and final offsets are
  consistent, without claiming that a truncated excerpt proves absence from
  an entire document or corpus

`contradictory` additionally requires an actual applicable counter-witness,
with an exact span in the emitted excerpt and a reason demonstrable there.
Counter-witnesses from filenames, metadata or text omitted by the final budget
are not accepted. An `unknown` label, an empty requirement list or a blanket
`related_only` label cannot create a zero.

These checks concern necessary literal witnesses, not entailment, truth of an
assertion, authority, approval or permission. The metric adapter does not
import the product's classifier to grade its output. Recognized policies are
pinned to the six literal necessary checks of policy v1; an unrecognized
extension requires an explicit operationalization update, not silent trust.

## Positive sufficiency remains an independent gate

Success@5, true Recall@5 and nDCG@10 retain their original frozen relevance
judgments and logical-document deduplication. Related or contradictory material
never proves a positive answer. A positive context is provably sufficient only
when a valid `evidence_candidate` cites a grade-3 gold document and contains its
entire frozen factual body. This conservative rule uses existing whole-resource
judgments, not newly invented query-specific answer spans.

A shorter prefix may in reality be useful, but the current frozen judgments do
not prove its sufficiency, so the adapter reports
`positive_sufficiency_unknown` and leaves that gate unsatisfied. Likewise,
all-abstention cannot pass. This is not a claim that the product failed when a
fragment is merely unverified, and it is not permission to adjust the reserved
queries or disclose their individual failures for tuning.

Reserved acceptance under the explicitly selected v2 supplement requires the
original retrieval thresholds, all eight positive contexts proved sufficient,
zero unsupported sufficient evidence, zero unverified dispositions, valid
citations/locators and complete real-model requests. Partial coverage for
unavailable, out-of-scope media owners remains reported honestly and does not
silently become complete coverage.
