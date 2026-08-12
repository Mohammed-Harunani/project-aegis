# Phase 3.1.2 -- Pure Verified Type-Conversion Engine: Implementation Report

**Status: Stage 3.1.2 implementation candidate complete and independently
pure-tested. Phase 3.1 is NOT complete.** Owner-local verification and one
canonical Stage 3.1.2 commit are still required before this stage closes.
Surgeon integration, governance, PostgreSQL verification, and live enablement
remain separate later stages.

## 1. Implemented module structure

```text
src/type_repair/
    __init__.py     -- public API surface
    models.py       -- ConversionStatus, ReasonCode, ConversionDiagnostic,
                       ColumnConversionResult
    policy.py       -- ConversionPolicy, DEFAULT_POLICY (version
                       phase3.1-policy-v1)
    converter.py    -- analyze_and_convert() and the per-target analyzers

tests/
    test_type_repair_converter.py  -- 149 pure-Python tests
```

No third-party dependency was added. Only the standard library
(`dataclasses`, `enum`, `decimal`, `math`, `re`, `types`) plus pandas
and NumPy (already project dependencies) are used.

## 2. Public API

```python
from type_repair import (
    analyze_and_convert,
    ColumnConversionResult,
    ConversionDiagnostic,
    ConversionStatus,
    ReasonCode,
    ConversionPolicy,
    DEFAULT_POLICY,
)

result = analyze_and_convert(series, "int64")          # DEFAULT_POLICY
result = analyze_and_convert(series, "int64", policy=DEFAULT_POLICY)
```

`analyze_and_convert` contains ordinary internal failures and returns
`ERROR / INTERNAL_CONVERSION_ERROR`. Executable conversions accept an exact
`pandas.Series`, an exact built-in target string, and an exact validated
`ConversionPolicy`; untrusted subclasses and duck-typed policy objects are
not executed.

## 3. Exact target matrix

Supported target spellings, exactly: `int64`, `float64`, `bool`,
`object`. Only an exact built-in `str` is accepted at the target boundary;
string subclasses cannot override `strip()`/`lower()` to redirect a
conversion. Normalization trims whitespace and lowercases for comparison,
**except** for a small, explicit blocklist of pandas/numpy
extension-dtype spellings (`Int64`, `Float64`, `Boolean`) which are
checked case-sensitively *before* any lowercasing and never treated as
equivalent to the plain numpy targets, even though they would
naive-lowercase-match one. **This exception exists because of a real
bug caught by this engine's own test suite**: the first draft's
normalization lowercased everything unconditionally, so `"Int64"`
(pandas' nullable integer extension type) silently resolved to
`"int64"` and was wrongly accepted. Fixed before this handoff, with a
dedicated regression test
(`test_extension_dtype_spellings_never_fold_into_supported_targets`).

Everything else -- `decimal`, `date`, `datetime`, `datetime_tz`,
`uuid`, `json`, `string`, `str`, `category`, and any other pandas
dtype not in the four above -- is `UNSUPPORTED`.

### 3.1 `int64`
- Accept: exact Python integers and every genuine platform NumPy integer
  scalar class (excluding bool and `numpy.timedelta64`) within
  `[-9223372036854775808, 9223372036854775807]`; finite floats whose
  mathematical value is exactly integral and in range; strings
  matching `^[+-]?(0|[1-9][0-9]*)$` (no leading zeros beyond a bare
  `"0"`, no whitespace, no decimal point, no exponent).
- Reject: fractional floats (`FRACTIONAL_VALUE`), out-of-range values
  (`OUT_OF_RANGE`), non-finite numbers (`NON_FINITE_NUMBER`), malformed
  strings (`PARSE_ERROR`), booleans and other objects
  (`UNSUPPORTED_SOURCE_VALUE`).

### 3.2 `float64`
- Accept: finite Python/NumPy floats as-is; integers only when the
  int -> float64 -> int round trip reproduces the exact integer
  (protects precision beyond `2**53`); decimal strings matching
  `^[+-]?(0|[1-9][0-9]*)([.][0-9]+)?$` only when the exact `Decimal`
  parse of the string equals `Decimal(float(string))` exactly --
  deliberately rejects `"0.1"`, since `Decimal(0.1) != Decimal("0.1")`
  (binary64 cannot represent 0.1 exactly).
- Reject: malformed/scientific-notation/comma/underscore strings
  (`PARSE_ERROR`), non-finite values or tokens (`NON_FINITE_NUMBER`),
  any precision-losing integer or decimal conversion
  (`PRECISION_LOSS`), other objects (`UNSUPPORTED_SOURCE_VALUE`).

### 3.3 `bool`
- Accept: native bool; integer `0`/`1` (not float, not any other
  integer); strings `true`/`false`/`0`/`1`, case-insensitive, after
  trimming ASCII whitespace only (space, tab, CR, LF, form feed,
  vertical tab).
- Reject everything else, including `2`, `-1`, `1.0`, `0.0`, `"yes"`,
  `"no"`, `"on"`, `"off"`, `""`, arbitrary strings -- all
  `AMBIGUOUS_BOOLEAN`. Python `bool()` and pandas `.astype(bool)` are
  never called as the validator anywhere in this engine.

### 3.4 `object`
- Non-transforming widening only: values keep their own identity
  (mutable references preserved, nothing stringified), row
  count/order/index/name preserved, a new Series is returned (input
  never mutated). A defensive round-trip equality check exists per
  value (`ROUND_TRIP_MISMATCH` if it ever fails) though no case in the
  test suite exercises controlled structural and semantic fault seams to
  prove the safety net rejects changed length, metadata, identity, or values.

## 4. Null policy

`None`, `pandas.NA`, `NaN`, `NaT`, and `numpy.timedelta64("NaT")` are
recognized as null. Null probing is non-throwing for ordinary exceptions:
unsupported objects whose pandas protocols fail are treated as non-null and
then classified normally rather than becoming internal errors.

- `int64`, `bool`: any null present makes the whole column
  `UNSAFE / NULL_TARGET_INCOMPATIBLE` -- one diagnostic per null row,
  `row_index` set, no converted series.
- `float64`, `object`: null positions are preserved exactly, counted in
  `null_count`, and are not treated as failures.

## 5. Diagnostic and outcome model

`ConversionStatus`: `SAFE`, `UNSAFE`, `UNSUPPORTED`, `ERROR` (str enum).

`ReasonCode` (str enum), all twelve required values implemented:
`PARSE_ERROR`, `FRACTIONAL_VALUE`, `OUT_OF_RANGE`, `PRECISION_LOSS`,
`AMBIGUOUS_BOOLEAN`, `NON_FINITE_NUMBER`, `NULL_TARGET_INCOMPATIBLE`,
`TIMEZONE_LOSS` (reserved, unused until a timezone-aware target
exists), `UNSUPPORTED_SOURCE_VALUE`, `UNSUPPORTED_TARGET_DTYPE`,
`ROUND_TRIP_MISMATCH`, `INTERNAL_CONVERSION_ERROR`.

`ColumnConversionResult` counting semantics: `total_count` is always
the complete series length; `null_count` is always the original
null-position count. `converted_count`/`failed_count` are computed
per row based on whether that row individually passes its own check --
true even when the overall status is not `SAFE`, since "3 of 100 rows
failed" is more informative than collapsing to zero.

**`total_count = null_count + converted_count + failed_count` does
NOT hold in general** -- confirmed directly, and corrected in this
handoff after an earlier draft of this document incorrectly claimed it
did. For a null-incompatible target (`int64`, `bool`), a null row is
counted in BOTH `null_count` and `failed_count` (it carries its own
`NULL_TARGET_INCOMPATIBLE` diagnostic as a rejected row, in addition to
being a null in the original input). Concretely, for
`pandas.Series([1, None]) -> int64`: `total_count=2`, `null_count=1`,
`converted_count=1`, `failed_count=1` -- the sum (3) exceeds
`total_count` (2) by design, not by accident. For a null-preserving
target (`float64`, `object`), a null row is counted only in
`null_count`, and the equation happens to hold in that case. Both
cases are covered by dedicated tests
(`test_count_semantics_for_null_incompatible_target`,
`test_count_semantics_for_null_preserving_target`).

`diagnostics` is an immutable tuple, in original row order.
`converted_series` is populated only when `status == SAFE`, and always
`None` otherwise -- there is no partially-converted result.

## 6. Tests run and results

Real pytest commands executed against this exact package:

```text
python -m pytest -q tests/test_type_repair_converter.py
149 passed

python -m pytest -q \
  tests/test_inspector.py \
  tests/test_consultant.py \
  tests/test_surgeon.py \
  tests/test_selector.py \
  tests/test_approval.py \
  tests/test_schema_registry_logic.py \
  tests/test_live_execution_logic.py \
  tests/test_dataset_codec.py \
  tests/test_type_repair_converter.py
218 passed

python -m pytest -q
218 passed, 3 skipped

python -m compileall -q src tests
exit code 0
```

The three skips are PostgreSQL integration tests protected by the required
`TEST_DATABASE_URL` guard. No PostgreSQL-backed result is claimed in this
package. The canonical Phase 2.5 database verification remains separate.

## 7. Bug found and fixed during this stage

A real defect was caught by this engine's own test suite before
handoff, not found afterward: naive case-insensitive target
normalization silently accepted `"Int64"` (pandas' nullable extension
dtype) as equivalent to `"int64"`. Fixed with an explicit,
case-sensitive blocklist checked before folding case -- see Section 3
above and `test_extension_dtype_spellings_never_fold_into_supported_targets`.

## 7a. Corrections made in response to external review

A subsequent review, verified directly against this code before
accepting, found three genuine defects and one documentation error,
all confirmed and fixed:

1. **Numeric strings with a trailing newline were silently accepted.**
   Confirmed directly: Python's `re` module treats a `$`-anchored
   pattern's `match()` as matching immediately before a trailing
   `\n`, even without `re.MULTILINE` -- so `"1\n"` passed the int64
   grammar and `"1.0\n"` passed the float64 grammar, both silently.
   Fixed by switching `match()` to `fullmatch()` in
   `policy.match_integer_string`/`match_decimal_string`, which
   requires the entire string to match with no such exception.
2. **Extended-precision NumPy floats could silently lose data.**
   Confirmed directly with `numpy.longdouble(1) + numpy.longdouble(2)
   ** numpy.longdouble(-63)` (128-bit precision on this platform):
   reducing to Python `float` (float64) before checking integrality or
   precision made this genuinely non-integral value look exactly `1.0`,
   since float64 cannot represent the tiny fractional component.
   Fixed by checking finiteness, integrality, and float64 round-trip
   fidelity using the value's own native numpy precision (`np.isfinite`,
   `value == np.floor(value)`, and reconstructing via the value's own
   type after a float64 round-trip) *before* ever reducing to Python
   float. Now correctly returns `FRACTIONAL_VALUE` for `int64` and
   `PRECISION_LOSS` for `float64`.
3. **`ConversionPolicy` was externally mutable through
   constructor-supplied mappings.** Confirmed directly: a caller-owned
   `dict` passed as `bool_tokens` was retained by reference, so
   mutating it after constructing the policy silently changed the
   "frozen" policy's actual behavior. Fixed with `__post_init__`
   (using `object.__setattr__`, the standard pattern for frozen-dataclass
   post-init normalization) that defensively copies `bool_tokens` into
   a new `MappingProxyType` and `supported_targets` into a new
   `frozenset`, regardless of whether the caller used the default
   factory or supplied their own mapping/set explicitly.
4. **A false documented invariant.** `total_count = null_count +
   converted_count + failed_count` was claimed but does not hold for
   null-incompatible conversions -- confirmed directly with
   `pandas.Series([1, None]) -> int64` (`1 + 1 + 1 = 3 ≠ 2`). The
   counting *behavior* was correct and useful; only the false equation
   was removed, replaced with an accurate description of the real
   overlap (a null-incompatible row is counted in both `null_count`
   and `failed_count` by design). See Section 6 above and
   `models.py`'s `ColumnConversionResult` docstring.

All four were verified directly against the actual running code
before being accepted as real, and again after fixing, before this
handoff -- not accepted on the strength of the review's claims alone.
Regression tests for all four are in the new
"External review corrections" section of
`tests/test_type_repair_converter.py`. Full suite re-run after all
four fixes: no regressions.

## 7b. Final corrections (second external review round)

A second review round, again independently verified against this code
before accepting, found two more real defects and several mandatory
test/documentation gaps:

1. **Valid extended-precision integers were wrongly rejected from
   `int64`.** The prior round's fix required a float64 round-trip
   before accepting an integral float for *any* target, including
   `int64` -- but `int64` conversion never actually depends on float64
   representability at all. Confirmed directly:
   `int(numpy.longdouble(2**53 + 1))` and
   `int(numpy.longdouble(9223372036854775807))` both preserve full
   native precision with no lossy intermediate, so requiring a float64
   round-trip first was itself an over-correction, wrongly rejecting
   exact, in-range integers. Fixed by computing the integer directly
   from the value's own native precision for `int64` specifically,
   once finiteness and integrality are confirmed -- the float64
   round-trip check remains correct and necessary only for the
   `float64` target, where the output genuinely must be an exact
   float64 value.
2. **Invalid `ConversionPolicy` configurations were still possible.**
   Confirmed directly: a caller could construct a policy with a
   non-bool token value (`{"true": 1}`), an expanded or inverted
   boolean mapping (`"yes"` added, or `"true"` mapped to `False`), or
   a `supported_targets` set including a dtype with no registered
   analyzer (reaching `ERROR / INTERNAL_CONVERSION_ERROR` instead of
   the correct `UNSUPPORTED`). Fixed by validating, in
   `__post_init__`, that for policy version `phase3.1-policy-v1` (the
   only version this implementation defines), `bool_tokens` and
   `supported_targets` must match their locked values exactly --
   raising `ValueError` at construction time otherwise. This makes the
   boolean token table and supported-target set genuinely locked parts
   of the safety contract, not caller-configurable options.

Additional test-portability issue addressed: the extended-precision
tests from the prior round assumed `numpy.longdouble` is wider than
`numpy.float64`, which is not true on every platform (some Windows
NumPy builds in particular). Tests now guard on
`numpy.finfo(numpy.longdouble).nmant > numpy.finfo(numpy.float64).nmant`
and are no-ops (not failures) where that doesn't hold -- confirmed
directly this evaluates to `True` on the platform this work was done
on (63 mantissa bits vs. 52).

12 additional tests added (exact large longdouble to int64, non-finite
longdouble for both targets, `numpy.float16`/`numpy.float32` safe
conversion, invalid boolean-token-value rejection, expanded/inverted-
token rejection, unsupported-target-in-policy rejection,
`DEFAULT_POLICY` construction stability, and all four count scenarios
named explicitly). One existing test
(`test_conversion_policy_supported_targets_not_externally_mutable`)
was corrected to construct from the complete, valid target set, since
the new validation correctly rejects the incomplete set it had
previously (and incidentally) used.

Final count after all four correction rounds: 102 type_repair tests
(97 after the third round + 5 new this round), 171 total pure-Python
tests project-wide. See Section
6 above for the corrected final numbers, which supersede any earlier
count in this document.

## 7c. Closure corrections (third external review round)

A third review round, again independently verified against this code
before accepting, found one **critical** defect and one immutable-model
gap:

1. **CRITICAL: policy-lock validation could be bypassed entirely by
   passing a non-default `version` string.** All validation added in
   the second correction round was gated behind
   `if self.version == POLICY_VERSION`. Confirmed directly that
   constructing `ConversionPolicy(version="other", ...)` bypassed
   every rule -- including, critically, widening `int64_max` by
   exactly one: `9223372036854775808` (`INT64_MAX + 1`) then reported
   `SAFE` with the value silently wrapped to
   `-9223372036854775808`. Also confirmed: forbidden boolean tokens,
   an unsupported target reaching `ERROR/INTERNAL_CONVERSION_ERROR`,
   and weakened numeric grammars were all reachable the same way.
   Fixed: validation is now unconditional, and `version` itself is
   locked -- constructing with anything other than `POLICY_VERSION`
   is rejected outright, since there is no alternate policy version
   this engine actually implements. `int64_min`/`int64_max` and both
   regex patterns are now also explicitly validated (they were not
   checked at all in the prior round, version-gated or not).
2. **`ColumnConversionResult` was not genuinely immutable.**
   Confirmed directly: passing a `list` for `diagnostics` let a
   caller mutate the original list after construction and change the
   supposedly-frozen result. Fixed with `__post_init__` defensively
   copying `diagnostics` into an actual tuple, and enforcing that
   `SAFE` requires a `converted_series` while every other status
   forbids one.

11 new tests added covering: the version-bypass fix directly (and that
it cannot be used to reach an unsupported target, a replaced integer
grammar, or a replaced decimal grammar), the exact `int64_max`
wraparound scenario end-to-end (confirming the tampered policy is now
unconstructable, not merely that the resulting conversion would be
wrong), both regex locks independent of the version check, diagnostics
tuple-copying, and both `SAFE`/non-`SAFE` `converted_series`
consistency rules.

Documentation corrected: the stale "60 pure-Python tests" reference in
Section 1's module structure was several rounds out of date; the
`a692e3f` git-comparison reference in Section 8 was corrected to
`4841a48`, the actual commit this sandbox's diff was run against (the
baseline-hash discrepancy itself remains unresolved and documented,
but the recorded command must reflect what was actually run, not an
unavailable hash). At the end of this third correction round, all
test counts throughout this document read 97 / 166. Two further
correction rounds have since run; see Sections 7d and 7e and Section 6
above for the actual final counts (102 targeted, 171 total), which
supersede this figure.

## 7d. Final-gate corrections (fourth external review round)

A fourth review round, independently verified against this code before
accepting, found two remaining gaps -- both requested in the previous
round's brief but not actually implemented then:

1. **Regex matcher type and flags were not locked.** The third round's
   fix compared `.pattern` text only. Confirmed directly:
   `re.compile(INTEGER_STRING_PATTERN.pattern, re.MULTILINE)`
   constructed successfully despite different actual matching
   behavior, and -- more seriously -- a duck-typed object exposing
   matching `.pattern`/`.flags` attributes but a `fullmatch()` that
   unconditionally returned `True` bypassed the grammar completely
   (`"01"` reported `SAFE` for `int64`). Fixed by validating that the
   supplied matcher is a genuine `re.Pattern` instance
   (`isinstance(..., re.Pattern)`) before ever trusting its
   `.pattern`/`.flags` attributes, and that `.flags` matches the
   canonical value exactly, not just the pattern text.
2. **Converter dispatch trusted `policy.is_supported_target()` alone.**
   Confirmed directly with a policy-like object claiming an
   unregistered target ("decimal") was supported: this reached a raw
   `KeyError` on the analyzer registry, surfacing as
   `ERROR/INTERNAL_CONVERSION_ERROR` instead of the correct
   `UNSUPPORTED/UNSUPPORTED_TARGET_DTYPE`. Fixed by adding an
   independent, hardcoded `_CONVERTER_KNOWN_TARGETS` set in
   `converter.py` itself, checked in addition to (not instead of) the
   policy's own opinion, plus a second explicit membership check
   immediately before indexing `_VALUE_ANALYZERS`.

5 new tests: regex flag-mismatch rejection for both patterns, the
non-`re.Pattern` duck-typing bypass rejected for both patterns, the
exact `"01"` bypass scenario confirmed unconstructable end-to-end, the
malformed-policy dispatch scenario confirmed correctly resolving to
`UNSUPPORTED`, and a regression guard confirming the new defensive
check doesn't disrupt normal operation for any of the four genuinely
supported targets.

Final count after this round: 106 type_repair tests (102 from the
fourth round + 4 new), 175 total pure-Python tests project-wide.

## 7e. Owner-verification corrections (fifth external review round)

A fifth review round, independently verified against this code before
accepting, found one runtime determinism defect, one error-containment
gap, and one stale documentation sentence:

1. **Long canonical integer strings became internal errors instead of
   `OUT_OF_RANGE`.** Confirmed directly: Python 3.11+ limits
   unrestricted `int(str)` conversion via
   `sys.get_int_max_str_digits()` (4300 by default). A canonical
   integer string beyond that length -- despite being simply out of
   `int64` range, nothing more -- raised `ValueError` internally,
   surfacing as `ERROR/INTERNAL_CONVERSION_ERROR`. Worse, confirmed
   this threshold is process-wide, mutable state: lowering the limit
   to its minimum allowed value (640) moved the exact digit count at
   which this happened, violating the determinism requirement (spec
   4.6) outright. Fixed with `_int64_from_canonical_string`, which
   determines range purely from digit length and, only exactly at the
   19-digit boundary, a lexicographic comparison against the known
   boundary strings -- never calling `int()` on a string longer than
   20 characters, comfortably below any valid value of that limit.
2. **`analyze_and_convert()` could still raise to its caller.**
   Confirmed directly: its own fallback error-containment path read
   `policy.version` unconditionally; a policy object lacking that
   attribute caused `analyze_and_convert()` itself to raise
   `AttributeError`, completely defeating its documented "never raises
   to the caller" guarantee. Fixed by guarding this read the same way
   every other field in that fallback path already was, using
   `"unknown"` when it can't be determined.
3. **A stale sentence contradicted the actual final counts.** Section
   7c's closing sentence ("all test counts throughout this document
   now read 97/166, superseding every earlier figure") was correct
   when written but had not been updated through two further
   correction rounds, directly contradicting the 102/171 (now 106/175)
   figures documented elsewhere. Rewritten as historical context
   rather than removed, since it accurately described that round's
   state at the time.

4 new tests: long-string-beyond-digit-limit resolving to `OUT_OF_RANGE`
across multiple digit counts, the process-wide-limit-independence
check (confirmed identical outcome whether the limit is default or set
to its minimum), a regression guard confirming the genuine boundary
values still succeed/fail correctly, and the malformed-policy-missing-
version scenario confirmed to no longer raise.

Final count after this round: 106 type_repair tests (102 from the
fourth round + 4 new), 175 total pure-Python tests project-wide.

## 7f. Local-verification corrections (sixth external review round)

A sixth review round, independently verified against this code before
accepting, found two more real defects:

1. **`numpy.timedelta64` was misclassified as an approved integer.**
   Confirmed directly: `isinstance(numpy.timedelta64(1, "D"),
   numpy.integer)` is `True` -- a genuinely surprising NumPy quirk. Left
   unexcluded, a time duration was silently treated as an approved
   numeric integer and failed unpredictably deeper in the pipeline,
   surfacing as `ERROR/INTERNAL_CONVERSION_ERROR` rather than the
   correct `UNSUPPORTED/UNSUPPORTED_SOURCE_VALUE`. Fixed with an
   explicit `_is_timedelta_like` exclusion in `_is_integer_like`, so a
   `numpy.timedelta64` value is now correctly rejected as unsupported
   for all three scalar targets.
2. **CRITICAL: policy field validation relied on comparison operators,
   which a malicious object could override.** Confirmed directly with
   a custom `EvilMax` class whose `__eq__`/`__ne__`/`__ge__` always
   return attacker-chosen results: `ConversionPolicy(int64_max=EvilMax())`
   constructed successfully despite the existing `!=`-based validation,
   and `INT64_MAX + 1` then reported `SAFE` with the value silently
   wrapped to `INT64_MIN` -- the same class of critical defect fixed
   in an earlier round, now reached through an entirely different
   mechanism (comparison-operator overriding rather than a bypassable
   version string). Fixed with exact built-in type checks
   (`type(x) is int`/`type(x) is str`/etc.) performed *before* any
   comparison operator is used on these values at all, for `version`,
   `int64_min`, `int64_max`, every `bool_tokens` key and value, and
   every `supported_targets` entry. `type()` identity checks are used
   deliberately instead of `isinstance()`, since a malicious subclass
   of `int` or `str` overriding its own comparison methods would still
   pass an `isinstance` check while remaining just as exploitable.
   Additionally, the converter itself now independently enforces the
   canonical `int64` boundaries using hardcoded `INT64_MIN`/`INT64_MAX`
   constants (`_is_within_int64_range`), in addition to whatever the
   policy claims -- so the converter's own safety no longer rests on
   trusting the policy object alone, the same defense-in-depth
   principle already applied to target dispatch in an earlier round.

6 new tests: `timedelta64` rejection across all three targets, a
regression guard confirming genuine NumPy integers are unaffected, the
exact-type validation rejecting the `EvilMax`-style bypass for
`int64_max`/`int64_min`/`version` directly, the end-to-end wraparound
scenario confirmed unconstructable, exact-type validation for
`bool_tokens` keys, and the converter's own defensive range check
(`_is_within_int64_range`) tested directly against the boundary
values.

Final count after this round: 112 type_repair tests (106 from the
fifth round + 6 new), 181 total pure-Python tests project-wide.

## 7g. Canonical-ready corrections (seventh external review round)

A seventh review round, independently verified against this code
before accepting, found three remaining safety gaps -- adversarial
testing specifically targeting the classifier and validation logic's
reliance on `isinstance()` and comparison operators:

1. **Primitive subclasses could silently alter values.** Confirmed
   directly: `EvilInt(int)` overriding `__int__` to return `0` made
   `EvilInt(1)` report `SAFE` with output `0` for `int64`, `0.0` for
   `float64`, and `False` for `bool` -- the actual value (`1`) was
   never what got produced. Similarly for a `float` subclass
   overriding `__int__`, and a `str` subclass overriding `strip()`/
   `lower()` to make `"yes"` pass as `"true"`. Fixed by replacing
   every broad `isinstance()` classifier with exact `type()`
   membership against explicit, hardcoded sets of the only trusted
   Python/NumPy scalar types (`_APPROVED_INTEGER_TYPES`,
   `_APPROVED_FLOAT_TYPES`, `_APPROVED_BOOL_TYPES`, and a direct
   `type(value) is str` check) -- there is no legitimate reason a
   genuine source value would ever need to be a subclass of any of
   these.
2. **The regex pattern-text comparison could itself be defeated by a
   lying `str` subclass.** Confirmed directly: even though the prior
   round's `isinstance(pattern, re.Pattern)` check correctly rejected
   a fake matcher object, `re.compile()` itself preserves whatever
   `str` (or `str` subclass) instance it's given as `.pattern` --
   a subclass with `__eq__`/`__ne__` always returning attacker-chosen
   results passed the text-equality check regardless of its actual
   content, letting `"01"` and `"1e3"` through. Fixed by requiring
   `type(pattern.pattern) is str` before ever comparing pattern text.
3. **CRITICAL: no final, independent guard existed immediately before
   `int64` materialisation.** Using a controlled internal analyzer
   fault (simulating a hypothetical future bug in a per-value
   analyzer) returning `INT64_MAX + 1`, the value reached
   `.astype("int64")` completely unchecked and silently wrapped to
   `INT64_MIN`, reporting `SAFE` -- the third distinct mechanism, across
   three different rounds, by which this same wraparound has been
   reachable. Fixed with a final check immediately before
   materialisation verifying every produced value has exact type `int`
   and falls within the canonical `int64` boundaries; any failure here
   resolves to `ERROR` (an internal inconsistency, not a normal
   per-value rejection, since the per-value analyzer was already
   supposed to guarantee this).

15 new tests: each subclass-override scenario individually (int,
float, str), a regression guard confirming every genuinely approved
NumPy integer/float width is unaffected, the regex pattern-text
subclass bypass for both grammars plus the exact `"01"` scenario
confirmed unconstructable end-to-end, the final-materialisation guard
exercised via two distinct controlled faults (out-of-range value,
wrong-type value) plus a regression guard for the genuine boundary
values, and expanded `timedelta64` coverage: `ns`/`s`/`D` units
individually, `NaT` (confirmed to correctly resolve through null
handling rather than being misclassified as an unsupported value in
its own right, per the spec's own null policy), a mixed valid-numeric-
and-timedelta column with exact atomic-rejection counts, and every
signed/unsigned NumPy integer width tested individually as a
regression guard.

Final count after this round: 127 type_repair tests (112 from the
sixth round + 15 new), 196 total pure-Python tests project-wide.

## 7h. Primary-developer completion hardening

The primary-developer pass fixed the three remaining independently reproduced
issues and added broader defence-in-depth checks:

1. **Trusted target boundary.** Target dtype objects must be exact built-in
   strings. A string subclass can no longer redirect `decimal` or `float64`
   into `int64`, and a target whose virtual string methods raise cannot escape
   containment. Non-string and untrusted target objects return
   `UNSUPPORTED_TARGET_DTYPE`.
2. **Complete platform NumPy integer coverage.** Approved integer scalar
   classes are discovered from NumPy's genuine scalar registry using exact
   type identity. Distinct `numpy.longlong` and `numpy.ulonglong` classes are
   supported where present, while `numpy.bool_`, `numpy.timedelta64`, and all
   user-defined subclasses remain excluded.
3. **Safe null probing.** Both `pandas.isna()` and scalar truth conversion are
   protected against ordinary exceptions. Unsupported protocol-heavy objects
   now return `UNSUPPORTED_SOURCE_VALUE` for scalar targets and widen safely
   to `object` with identity preserved.
4. **All scalar materialisation boundaries hardened.** `int64`, `float64`, and
   `bool` outputs are validated for exact type, finiteness/range, null-count
   consistency, metadata preservation, dtype, and post-materialisation value
   equality before a `SAFE` result is returned.
5. **Policy and input objects locked.** Public policy string helpers reject
   subclasses; only an exact `ConversionPolicy` may authorize an executable
   target; a malformed policy can still receive a precise unsupported-target
   result but cannot execute parsers or converters. A `pandas.Series` subclass
   is not executed.
6. **Object widening invariants.** The engine verifies a new ordinary Series,
   identical length/index/name/order, object dtype, null positions, and value
   identity/equality. Controlled fault tests cover structural mismatch paths.

This pass added 22 tests over the 127-test input package. Current authoritative
results are **149 targeted tests**, **218 complete available pure tests**, and
**218 passed / 3 PostgreSQL-gated skips** for the complete repository suite.

## 8. Known limitations

1. This stage is pure and standalone. Surgeon does not yet call this
   engine; `CAST_COLUMN` in Surgeon still uses the pre-existing
   `.astype()` path, unchanged, in sandbox mode only.
2. **Live `CAST_COLUMN` remains completely blocked** --
   `/simulate-migration-from-source` still rejects every `CAST_COLUMN`
   repair before ticket creation, exactly as established in Phase
   2.5/3.1.1. Nothing in this stage touches that rejection, `app.py`,
   `src/live_execution/`, governance/approval repositories, database
   models, Alembic migrations, Docker files, environment templates, or
   Phase 2.1--2.5 documentation. The complete six-file Stage 3.1.2
   patch in `HANDOFF/GIT_DIFF.patch` was generated against the owner
   Stage 3.1.1 tree represented by canonical Phase 2.5 commit `4a5ead5`
   plus the authoritative specification committed locally as
   `a692e3f`. Owner application and commit remain pending.
3. The `ROUND_TRIP_MISMATCH` path for `object` targets is a defensive
   safety net. Controlled internal-fault tests verify structural mismatch
   handling; normal pandas object widening is expected to remain lossless.
4. `TIMEZONE_LOSS` is defined but unused -- no target in this stage's
   matrix (`int64`/`float64`/`bool`/`object`) involves timezones. It
   becomes relevant once `datetime_tz` conversions are addressed in a
   later, explicitly out-of-scope stage.
5. Real pytest and compile verification were executed successfully. The
   environment does not provide `TEST_DATABASE_URL`, so three PostgreSQL
   integration tests were skipped by their explicit safety guard. Stage 3.1.5
   remains the dedicated PostgreSQL verification stage.

## 9. Explicit confirmation

- Surgeon is unchanged. It does not import or call anything in
  `src/type_repair`.
- Live execution is unchanged. `execute_live`, the publication writer,
  reconciliation, and rollback logic in `src/live_execution/` are
  untouched.
- Live `CAST_COLUMN` remains blocked, exactly as it was before this
  stage.
- `src/type_repair` imports nothing from FastAPI, SQLAlchemy,
  repositories, or PostgreSQL connectors -- confirmed both by direct
  source inspection and by a dedicated test
  (`test_no_third_party_dependency_imports`).
- Phase 3.1 is **not** marked complete by this stage.
