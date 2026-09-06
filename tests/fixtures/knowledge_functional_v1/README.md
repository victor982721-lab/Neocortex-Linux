# Frozen functional retrieval fixtures

Development-only synthetic documents, not personal corpus material. The freeze
commits 40 files and 30 queries before ranking changes. DEV contains 24 files
(23 logical documents), 16 positive queries and 4 negative queries. The sealed
reserve contains 16 files, 8 positive queries and 2 negative queries, with
semantic families disjoint from DEV. The radiator incident belongs only to DEV.

`dev/manifest.json` pins bytes, logical identity and content revision, while
`dev/queries.json` contains source-grounded relevance grades. The PDF and DOCX
copies of the incident are one logical document, not two successes. Manuals,
mentions, explicit negation, absent facts and cross-domain meanings provide
distractors. No real person, document or industrial asset is asserted here.

`freeze.json` pins both partitions and `reserve.tar.aes`. Only the evaluation
steward opens the reserve, using a local key kept outside Git in the canonical
audit directory. Tuning agents must not decrypt it or receive reserve queries,
documents, individual results or errors that reveal their content. Aggregate
reserved results are disclosed only after the candidate SHA is frozen. A
failure is not permission to tune against that reserve; a fresh independent
reserve is required if failures are disclosed for subsequent development.

Run `tools/benchmark_knowledge_functional.py` explicitly against an immutable
installed launcher, with its exact SHA, the existing local model cache, and a
new private workspace. It creates isolated corpus/state/config paths, executes
real extraction/indexing/search/context, limits the entire run to 15 minutes
and each subprocess to 12 GiB address space, and leaves full logs in that
workspace. It never installs models, chooses the personal corpus, changes the
runtime configuration or runs a product quality gate. Fixtures injected into
unit tests prove orchestration/metrics only, never model quality.

Metrics use unique logical documents with pinned revisions, not chunks or
format aliases. Success@5 is the proportion of positive queries with at least
one relevant document; Recall@5 is the mean fraction of all relevant documents
retrieved among the first five. nDCG@10 uses frozen grades and exponential
gain. Negative unsupported evidence counts context selections for questions
whose answer is absent, and is never hidden by a changed denominator.

Reserved acceptance requires Success@5 >= 0.90 (therefore all 8/8 positives),
nDCG@10 no lower than the same frozen baseline, zero unsupported negative
evidence, valid citations/locators, successful requests and real vector queries.
Partial coverage remains explicit in the metrics and warnings, rather than
being reclassified as an execution failure merely because general retrieval
also asks for media owners absent from this document-only fixture collection.
An execution error, missing payload or non-real vector run still fails the
measurement, and index publication must cover all physical fixture files.
Locator checks cover immutable source bytes, the resource/revision/evidence
chain, source-backed snippets, section identity and structural coordinate
bounds, not an assertion of exact GUI coordinate resolution.

An explicitly selected context-v2 run uses the separately authorized
`OPERATIONALIZATION_V2.md` / `operationalization-v2.json` supplement. It keeps
the preceding v1 counter and immutable baseline visible while measuring
unsupported **sufficient** evidence separately from verified related material.
It does not change the frozen queries, relevance grades, split or reserve.
