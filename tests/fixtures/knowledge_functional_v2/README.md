# Independent replacement holdout R2

This is a new frozen dataset binding, not a revision of the original V1
documents, queries, judgments or results. The formal comparison corpus has
40 files: the 24 original DEV files referenced at
`../knowledge_functional_v1/dev` plus 16 sealed R2 files. Its 30 queries retain
the original 20 DEV queries and add 8 positive and 2 negative reserved queries.

R2 families are disjoint from both the original DEV families and retired R1,
with comparable observed-record/distractor pairs, formats, paraphrases,
misleading mentions and questions whose requested facts are absent. The
questions and judgments were fixed before scoring or post-R1 source tuning.
Only the evaluation steward may decrypt or inspect this reserve.

The same original installed d1 baseline and existing local model bytes must be
measured on this exact 40-file corpus. A candidate must have an explicitly
frozen installed SHA and separate root GO. Scores and individual results must
not guide adjustment of this reserve or replacement of difficult questions.

Use the existing development benchmark with this directory's `freeze.json`
and dataset-bound operationalization supplement. The shared metric code,
resource-level Success@5/Recall@5/nDCG@10 definitions, positive factual
sufficiency rule, 12k presentation budget and legacy counters are unchanged.
The original V1 freeze remains valid historical evidence, but R1 is retired
and cannot be run as an independent candidate gate.

The separate `knowledge_development_expanded_r1` view contains 40 files and
30 diagnostic queries after R1 retirement. It is not part of R2's independent
comparison corpus and must not be combined to inflate acceptance metrics.
