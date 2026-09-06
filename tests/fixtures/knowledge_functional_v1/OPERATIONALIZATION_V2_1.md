# Literal verifier coverage correction v2.1

This explicitly authorized developer-only supplement corrects one verification
coverage error before the first candidate-reserve run. It does not change the
dataset, questions, relevance judgments, split, models, retrieval metrics,
12,000-character default, legacy selection counter, or sufficient-evidence
rule established by v2.

The original v2 supplement and every earlier result remain immutable:

- `operationalization-v2.json`: SHA-256
  `6898c112bf670eec1d29b03881e3275407a6d5fa011d79618700cae7c34f4969`
- Original adapter: SHA-256
  `c6acac048f9ed649c8a678ed9ca8608c7fbaaae0442bcf6bce9c18fa2501e25d`,
  preserved in Git at `1718f2cc134da6f5840a025d20abf72086f141be` and in the
  canonical audit directory
- Original fixture freeze: SHA-256
  `02c43c7100db4493785b3bd69ae43358115e800050eac8ac1d0fff4817921ce9`

## Adjudicated observation

DEV Q09 asks about the first lift of `depósito L4`. The source-backed D16
witness says `El registro no corresponde al depósito L4`, at the exact emitted
character span. This explicitly excludes the same named subject from that
record's scope, without saying whether an event occurred or what condition
the asset had. The producer did not invent that relation.

The old verifier recognized only an identifier immediately after `a/al`, so
the intervening nominal head `depósito` caused an UNKNOWN despite a literal,
applicable witness. The case was also exposed at an explicitly diagnostic
100,000-character budget, with the source still visible, rather than treating
its omission at the normal budget as a fix. The frozen relevance judgment for
D16 remains unchanged: it is not an answer to Q09.

## Minimal independent verification rule

Exact final-excerpt text, offsets, source bytes and captured retrieval bindings
are still mandatory. Within that exact witness:

1. Recognize only one explicit `no corresponde a/al` exclusion cue, optionally
   with a Spanish article after `a`
2. The first identifier in the bounded target phrase must be one explicitly
   present in the query; never skip another identifier to reach a later match
3. A bare requested identifier keeps its existing interpretation. A qualified
   identifier requires one to three adjacent nominal words that occur with
   that identifier as the same contiguous phrase in the query
4. Do not cross punctuation, prepositions or another clause to associate the
   identifier. In particular, `proveedor de L4` does not designate `depósito
   L4`, and a different nominal head or identifier remains unverified
5. Hypothetical, conditional, denied or multiply negated statements remain
   unverified. These guards inspect the same final-excerpt sentence containing
   the exact witness, not only an internal span that could omit a preceding
   denial or condition. PDF soft-wrap newlines do not hide that context, while
   another sentence is not borrowed. This conservative matcher is not a
   general Spanish entailment parser, and complex constructions do not receive
   an automatic pass

The rule contains no fixture IDs, asset names, filenames or special case for
one industrial noun. Tests include distinct requested nominal subjects,
another identifier, another entity, a provider relationship, shifted or
altered spans, clause boundaries, double negation and conditional statements,
including exact internal spans whose denial or condition remains outside the
span but inside the same final-excerpt sentence.

## Reporting and scope

New grading identifies the v2.1 adapter and supplemental hash. Regraded reports
are separate files, retaining references to both versions. The 1718 DEV
presentation still fails positive sufficiency at 14/16 even when this false
UNKNOWN is corrected; v2.1 does not hide the actual packing regression.

The corrected producer's 20 DEV responses and its additional wide Q09 response
are checked independently. They are development projections over existing
captures, not new model runs, installed acceptance, or reserved evaluation.
Formal installed DEV and the candidate-reserve gate retain their separate
authorization and artifact-SHA barriers.
