"""
Aegis_TypeRepairPolicy
Phase 3.1.2 -- the locked conversion policy: signed 64-bit range,
strict boolean token table, canonical integer/decimal string grammars,
and target-dtype normalization. Version phase3.1-policy-v1 (spec
Section 7). Pure Python -- re, dataclasses, types only.
"""

import re
import types
from dataclasses import dataclass, field
from typing import Mapping


POLICY_VERSION = "phase3.1-policy-v1"

# Signed 64-bit range (spec Section 9.1).
INT64_MIN = -9223372036854775808
INT64_MAX = 9223372036854775807

# Supported target spellings, exactly these four (spec Section 7).
# Everything else -- decimal/date/datetime/datetime_tz/uuid/json,
# Int64/Float64/boolean/string/str/category, and anything not listed
# -- is UNSUPPORTED. This set is intentionally not extensible by
# passing a different string in; normalize_target_dtype() only
# canonicalizes case/whitespace, it never invents new supported names.
SUPPORTED_TARGETS = frozenset({"int64", "float64", "bool", "object"})

# Canonical integer-string grammar (spec Section 9.1): optional sign,
# then either a single "0" or a non-zero digit followed by any digits.
# No leading zeros other than "0" itself, no whitespace, no decimal
# point, no exponent, no thousands separator.
INTEGER_STRING_PATTERN = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")

# Canonical decimal-string grammar (spec Section 9.2): same integer
# part as above, with an optional ".<digits>" fractional part (at
# least one digit after the point if present). No scientific notation,
# no whitespace, no thousands separator, no locale-specific forms.
DECIMAL_STRING_PATTERN = re.compile(r"^[+-]?(0|[1-9][0-9]*)([.][0-9]+)?$")

# ASCII whitespace only (spec Section 9.3): space, tab, CR, LF, form
# feed, vertical tab. Deliberately not Python's broader str.strip()
# default (which also strips various Unicode whitespace) -- the spec
# is explicit about which characters count.
_ASCII_WHITESPACE_CHARS = " \t\r\n\f\v"

# Strict boolean token table (spec Section 9.3). Only ever consulted
# AFTER a value has already been confirmed to be a string and trimmed
# of ASCII whitespace, then lowercased -- native bool and native
# int(0/1) are handled directly by the converter, never routed through
# this string table.
_BOOL_TOKENS = {
    "true": True,
    "false": False,
    "1": True,
    "0": False,
}


# Known pandas/numpy extension-dtype spellings that must NEVER be
# treated as equivalent to a supported target, even though they would
# match one after naive case-folding (e.g. "Int64".lower() == "int64").
# These are genuinely different dtypes -- pandas' nullable Int64 is
# not numpy's int64 -- and silently conflating them was an explicit,
# real bug caught by this module's own test suite before it shipped.
# Checked case-SENSITIVELY, before any case-insensitive normalization
# is applied.
_BLOCKED_EXTENSION_DTYPE_SPELLINGS = frozenset({
    "Int64", "Float64", "Boolean",
})


def _strip_ascii_whitespace(s: str) -> str:
    return s.strip(_ASCII_WHITESPACE_CHARS)


@dataclass(frozen=True)
class ConversionPolicy:
    """
    Immutable. bool_tokens and supported_targets are defensively
    copied and frozen in __post_init__ regardless of whether the
    caller used the default factory or supplied their own mapping/set
    explicitly -- confirmed directly that without this, a
    caller-supplied dict was retained BY REFERENCE: mutating the
    original dict after constructing the policy silently changed what
    the "frozen" policy accepted, since frozen only prevents field
    REASSIGNMENT, not mutation of a mutable object a field happens to
    point at. object.__setattr__ is the standard, documented way to
    perform this kind of post-init normalization on a frozen
    dataclass -- it bypasses frozen's restriction only within
    __post_init__ itself, not afterward.
    """

    version: str = POLICY_VERSION
    int64_min: int = INT64_MIN
    int64_max: int = INT64_MAX
    bool_tokens: Mapping[str, bool] = field(
        default_factory=lambda: types.MappingProxyType(dict(_BOOL_TOKENS))
    )
    integer_string_pattern: "re.Pattern" = INTEGER_STRING_PATTERN
    decimal_string_pattern: "re.Pattern" = DECIMAL_STRING_PATTERN
    supported_targets: frozenset = field(default_factory=lambda: SUPPORTED_TARGETS)

    def __post_init__(self):
        # dict(self.bool_tokens) copies whatever mapping was supplied
        # (a plain dict, a MappingProxyType, or anything else
        # dict-like) into a NEW, independent dict before wrapping it
        # read-only -- this is what actually severs the reference to
        # any caller-owned mutable object.
        frozen_bool_tokens = types.MappingProxyType(dict(self.bool_tokens))
        frozen_supported_targets = frozenset(self.supported_targets)
        object.__setattr__(self, "bool_tokens", frozen_bool_tokens)
        object.__setattr__(self, "supported_targets", frozen_supported_targets)

        # Exact built-in TYPE checks, before any comparison operator
        # is used on these values at all. Confirmed directly this
        # matters: a custom object with its own __eq__/__ne__/__ge__
        # (e.g. one that always returns whatever the object's author
        # wants) can defeat comparison-based validation entirely --
        # ConversionPolicy(int64_max=EvilMax()) constructed
        # successfully and then let INT64_MAX + 1 report SAFE with the
        # value silently wrapped to INT64_MIN. type(x) is T is used
        # rather than isinstance(x, T), specifically to be airtight
        # against a subclass that might itself override comparison
        # behavior -- there is no legitimate reason any of these
        # fields would ever need to be anything other than the exact
        # built-in type.
        if type(self.version) is not str:
            raise ValueError(
                f"version must be an exact built-in str; got "
                f"{type(self.version).__name__}."
            )
        if type(self.int64_min) is not int:
            raise ValueError(
                f"int64_min must be an exact built-in int; got "
                f"{type(self.int64_min).__name__}. A custom object with its "
                f"own comparison methods could otherwise defeat the range "
                f"validation below entirely."
            )
        if type(self.int64_max) is not int:
            raise ValueError(
                f"int64_max must be an exact built-in int; got "
                f"{type(self.int64_max).__name__}. A custom object with its "
                f"own comparison methods could otherwise defeat the range "
                f"validation below entirely."
            )
        for key in frozen_bool_tokens.keys():
            if type(key) is not str:
                raise ValueError(
                    f"bool_tokens keys must be exact built-in str; got "
                    f"{key!r} of type {type(key).__name__}."
                )
        for token_value in frozen_bool_tokens.values():
            if type(token_value) is not bool:
                raise ValueError(
                    f"bool_tokens values must be exact built-in bool; got "
                    f"{token_value!r} of type {type(token_value).__name__}."
                )
        for entry in frozen_supported_targets:
            if type(entry) is not str:
                raise ValueError(
                    f"supported_targets entries must be exact built-in str; "
                    f"got {entry!r} of type {type(entry).__name__}."
                )

        # Validation is UNCONDITIONAL -- confirmed directly, twice now,
        # that gating it behind `if self.version == POLICY_VERSION`
        # created a trivial bypass: constructing with ANY other
        # version string skipped every check below entirely. That
        # allowed forbidden boolean tokens ("yes" accepted), an
        # unsupported target reaching a nonexistent analyzer
        # (ERROR/INTERNAL_CONVERSION_ERROR instead of UNSUPPORTED), a
        # weakened numeric grammar ("01" accepted for int64, "1e3"
        # accepted for float64), and -- critically -- silent int64
        # wraparound: tampering int64_max by one let
        # 9223372036854775808 (INT64_MAX + 1) report SAFE with the
        # actual output -9223372036854775808. There is exactly ONE
        # policy version this engine implements. A policy claiming to
        # be some OTHER version is not a different, legitimately
        # configured variant -- it is an unimplemented one, and
        # silently accepting it while skipping all safety validation
        # was itself the vulnerability. The version itself is
        # therefore locked too: constructing with anything other than
        # POLICY_VERSION is rejected outright, not silently permitted
        # under relaxed rules.
        if self.version != POLICY_VERSION:
            raise ValueError(
                f"Unsupported ConversionPolicy version {self.version!r}. Only "
                f"{POLICY_VERSION!r} is implemented -- there is no alternate "
                f"policy version to construct, so a different version string "
                f"cannot be used to bypass this policy's locked safety rules."
            )
        if dict(frozen_bool_tokens) != dict(_BOOL_TOKENS):
            raise ValueError(
                f"bool_tokens for policy version {POLICY_VERSION!r} must be "
                f"exactly {_BOOL_TOKENS!r}; got {dict(frozen_bool_tokens)!r}. "
                f"The boolean token table is a locked part of the safety "
                f"contract, not a caller-configurable option."
            )
        for token_value in frozen_bool_tokens.values():
            if not isinstance(token_value, bool):
                raise ValueError(
                    f"bool_tokens values must be actual bool instances; got "
                    f"{token_value!r} of type {type(token_value).__name__}."
                )
        if frozen_supported_targets != SUPPORTED_TARGETS:
            raise ValueError(
                f"supported_targets for policy version {POLICY_VERSION!r} must "
                f"be exactly {set(SUPPORTED_TARGETS)!r}; got "
                f"{set(frozen_supported_targets)!r}. Unknown targets must "
                f"remain UNSUPPORTED and must never reach a nonexistent "
                f"analyzer."
            )
        if self.int64_min != INT64_MIN or self.int64_max != INT64_MAX:
            raise ValueError(
                f"int64_min/int64_max are locked to the signed 64-bit range "
                f"[{INT64_MIN}, {INT64_MAX}] for policy version "
                f"{POLICY_VERSION!r}; got [{self.int64_min}, {self.int64_max}]. "
                f"Widening this range even by one, e.g. accepting "
                f"9223372036854775808 as 'in range', causes it to silently "
                f"wrap to -9223372036854775808 when built into the output "
                f"array -- confirmed directly, and exactly the kind of silent "
                f"corruption this engine exists to prevent."
            )
        if not isinstance(self.integer_string_pattern, re.Pattern):
            raise ValueError(
                f"integer_string_pattern must be a genuine re.Pattern instance "
                f"(from re.compile()) for policy version {POLICY_VERSION!r}; got "
                f"{type(self.integer_string_pattern).__name__}. A duck-typed "
                f"object exposing only .pattern/.fullmatch() could otherwise "
                f"bypass the grammar entirely regardless of what those "
                f"attributes claim -- confirmed directly with an object whose "
                f"fullmatch() unconditionally returned True."
            )
        if type(self.integer_string_pattern.pattern) is not str:
            raise ValueError(
                f"integer_string_pattern.pattern must be an exact built-in "
                f"str for policy version {POLICY_VERSION!r}; got "
                f"{type(self.integer_string_pattern.pattern).__name__}. "
                f"re.compile() preserves whatever str (or str subclass) "
                f"instance it is given as .pattern -- a subclass with a "
                f"lying __eq__/__ne__ could otherwise pass the text "
                f"comparison below regardless of its actual content."
            )
        if self.integer_string_pattern.pattern != INTEGER_STRING_PATTERN.pattern:
            raise ValueError(
                f"integer_string_pattern is locked for policy version "
                f"{POLICY_VERSION!r} and may not be replaced; got "
                f"{self.integer_string_pattern.pattern!r}."
            )
        if self.integer_string_pattern.flags != INTEGER_STRING_PATTERN.flags:
            raise ValueError(
                f"integer_string_pattern flags are locked for policy version "
                f"{POLICY_VERSION!r}; got {self.integer_string_pattern.flags!r}, "
                f"expected {INTEGER_STRING_PATTERN.flags!r}. Matching pattern "
                f"text with different flags (e.g. adding re.MULTILINE) is not "
                f"the same grammar and is not permitted."
            )
        if not isinstance(self.decimal_string_pattern, re.Pattern):
            raise ValueError(
                f"decimal_string_pattern must be a genuine re.Pattern instance "
                f"(from re.compile()) for policy version {POLICY_VERSION!r}; got "
                f"{type(self.decimal_string_pattern).__name__}."
            )
        if type(self.decimal_string_pattern.pattern) is not str:
            raise ValueError(
                f"decimal_string_pattern.pattern must be an exact built-in "
                f"str for policy version {POLICY_VERSION!r}; got "
                f"{type(self.decimal_string_pattern.pattern).__name__}."
            )
        if self.decimal_string_pattern.pattern != DECIMAL_STRING_PATTERN.pattern:
            raise ValueError(
                f"decimal_string_pattern is locked for policy version "
                f"{POLICY_VERSION!r} and may not be replaced; got "
                f"{self.decimal_string_pattern.pattern!r}."
            )
        if self.decimal_string_pattern.flags != DECIMAL_STRING_PATTERN.flags:
            raise ValueError(
                f"decimal_string_pattern flags are locked for policy version "
                f"{POLICY_VERSION!r}; got {self.decimal_string_pattern.flags!r}, "
                f"expected {DECIMAL_STRING_PATTERN.flags!r}."
            )

    def normalize_target_dtype(self, raw_target: str) -> str:
        """
        Normalize an exact built-in target string by trimming surrounding
        whitespace and comparing case-insensitively. The returned supported
        spelling is canonical lowercase.

        The exact-type check is a safety boundary, not cosmetic validation:
        a ``str`` subclass can override ``strip()`` or ``lower()`` and redirect
        a request for one dtype into another. Only an exact built-in ``str``
        is trusted here. The public converter handles untrusted/non-string
        target objects as ``UNSUPPORTED_TARGET_DTYPE`` before calling this
        method.

        Known pandas extension-dtype spellings such as ``Int64`` are checked
        before case-folding so they cannot silently collapse into the NumPy
        targets supported by this initial matrix.
        """
        if type(raw_target) is not str:
            raise TypeError(
                "target dtype must be an exact built-in str; "
                f"got {type(raw_target).__name__}."
            )

        trimmed = raw_target.strip()
        if trimmed in _BLOCKED_EXTENSION_DTYPE_SPELLINGS:
            return trimmed
        return trimmed.lower()

    def is_supported_target(self, normalized_target: str) -> bool:
        return (
            type(normalized_target) is str
            and normalized_target in self.supported_targets
        )

    def match_integer_string(self, s: str) -> bool:
        """
        No whitespace trimming here -- the integer-string grammar
        (spec Section 9.1) explicitly rejects " 1" and "1 ". Uses
        fullmatch(), not match(): confirmed directly that Python's re
        module treats a trailing $ as matching immediately before a
        final "\\n" even without re.MULTILINE, so match() with this
        $-anchored pattern was silently accepting "1\\n" as a valid
        integer string -- a real, confirmed bug, fixed here.
        fullmatch() requires the entire string to match, with no such
        exception.
        """
        if type(s) is not str:
            return False
        return bool(self.integer_string_pattern.fullmatch(s))

    def match_decimal_string(self, s: str) -> bool:
        """No whitespace trimming here either -- spec Section 9.2 is
        explicit that whitespace is not trimmed for decimal strings.
        fullmatch(), not match() -- same trailing-newline reasoning as
        match_integer_string()."""
        if type(s) is not str:
            return False
        return bool(self.decimal_string_pattern.fullmatch(s))

    def lookup_bool_token(self, s: str):
        """
        s must already be ASCII-whitespace-trimmed and lowercased by
        the caller before this lookup -- this function does the
        trimming defensively too, so it can't silently produce a
        wrong answer if a caller forgets, but the CONVERTER is still
        responsible for lowercasing before calling this (trimming
        alone doesn't change case). Returns True/False on a match, or
        None if the token isn't recognized.
        """
        if type(s) is not str:
            return None
        trimmed = _strip_ascii_whitespace(s)
        return self.bool_tokens.get(trimmed.lower())


DEFAULT_POLICY = ConversionPolicy()
