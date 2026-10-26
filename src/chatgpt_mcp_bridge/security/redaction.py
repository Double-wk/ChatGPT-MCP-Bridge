"""Credential redaction retained from the original SSH MCP implementation."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_REDACTION_PLACEHOLDER: str = "{REDACTED}"

# Distinct placeholder for the case where redaction consumed MORE than a
# credential value.
#
# Redaction replaces from the credential marker to the end of the
# whitespace-delimited token, which is what makes leaking a partially
# quoted secret impossible. Shell separators are not whitespace, so
# ``--password=X;touch /tmp/f`` redacted to the plain placeholder reads
# ``--password={REDACTED} /tmp/f`` — the ``touch`` verb is gone from the
# audit record, and ``--password=X;reboot`` loses ``reboot`` entirely
# while the command still RUNS. Reproduced end-to-end against a live host
# on 2026-09-07: the elided ``touch`` created its file.
#
# Narrowing the replacement to the separator was rejected: a secret
# containing ``;`` would then have its tail printed. So the value stays
# fully hidden and the RECORD stops being silent instead — an auditor
# reading ``{REDACTED+ELIDED}`` knows the token carried something beyond
# the secret and that the log is not a faithful transcript. This is a
# tripwire like the rest of this module: it cannot say WHAT was elided,
# only that something was.
_REDACTION_ELIDED_PLACEHOLDER: str = "{REDACTED+ELIDED}"

# Shell separators that can chain a second command onto a credential
# token. ``$(`` is listed explicitly because ``(`` alone is common in
# legitimate quoted values. Newline and CR are deliberately absent: both
# callers operate on tokens produced by ``_split_ws_preserving``, so
# whitespace has already split the text and neither can appear inside a
# token being elided — listing them would be an unreachable branch in a
# security-relevant predicate.
_SHELL_SEPARATOR_RE = re.compile(r"[;|&`]|\$\(")


def _placeholder_for(elided: str) -> str:
    """Pick the placeholder for text about to be replaced wholesale.

    The pipeline runs several rules over the SAME string, so a later rule
    can be handed text an earlier one already redacted. Found by CI's
    200-example Hypothesis profile 2026-09-07: rule 3a flagged
    ``--…-token=x&`` correctly and then ``_redact_long_flags`` re-matched
    the same flag and replaced ``{REDACTED+ELIDED}`` — which holds no
    separator — with the plain placeholder, silently DOWNGRADING the
    record. A marker that a later rule can erase is worse than no marker,
    so an existing one is preserved. The plain placeholder is not sticky:
    it carries no claim to lose.

    Args:
        elided: The exact text redaction is discarding.

    Returns:
        ``{REDACTED+ELIDED}`` if that text could chain another command, or
        already records that something was elided; otherwise the plain
        ``{REDACTED}``.
    """
    if _REDACTION_ELIDED_PLACEHOLDER in elided:
        return _REDACTION_ELIDED_PLACEHOLDER
    if _SHELL_SEPARATOR_RE.search(elided):
        return _REDACTION_ELIDED_PLACEHOLDER
    return _REDACTION_PLACEHOLDER


# Environment variables that are known credential sinks. Matched
# case-insensitively as ``<NAME>=<value>`` substrings.
_SECRET_ENV_NAMES: tuple[str, ...] = (
    # Database passwords
    "PGPASSWORD",
    "MYSQL_PWD",
    "REDIS_PASSWORD",
    "MONGODB_PASSWORD",
    "DB_PASSWORD",
    "DATABASE_PASSWORD",
    # Cloud providers
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GCP_API_KEY",
    "AZURE_CLIENT_SECRET",
    # Source control / CI
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "NPM_TOKEN",
    # Generic / OAuth
    "TOKEN",
    "API_KEY",
    "API_TOKEN",
    "SECRET",
    "SECRET_KEY",
    "BEARER_TOKEN",
    "ACCESS_TOKEN",
    "REFRESH_TOKEN",
    "CLIENT_SECRET",
    "PRIVATE_KEY",
)


def _long_flag_is_credential(flag: str) -> bool:
    """Return True if ``flag`` (e.g. ``--db-password``) names a credential.

    Defect 1 (panel iteration 2, all three reviewers): the previous regex
    tried to express "optional bounded junk, then one of these keywords"
    as a SINGLE pattern (``(?:[\\w-]{0,40}[-_])?(?:password|...)``), which
    forced a length bound on the junk to stay linear — the junk's
    character class (``[\\w-]``) overlaps the keyword alternation that
    follows it, so an unbounded possessive prefix would greedily swallow
    the keyword itself and never redact anything (see
    ``_redact_long_flags`` for why plain unbounded backtracking isn't safe
    either). That bound is exactly what let
    ``--aaa...(41 a's)...-password=Secret123`` leak past.

    Splitting the concern in two removes the tension: the flag NAME is
    captured with no length limit at all (see ``_redact_long_flags``), and
    this plain Python suffix check — which has no bound of its own —
    decides whether that name is credential-shaped.
    """
    body = flag[2:] if flag.startswith("--") else flag
    body_lower = body.lower()
    for kw in _LONG_FLAG_KEYWORDS:
        if (
            body_lower == kw
            or body_lower.endswith(f"-{kw}")
            or body_lower.endswith(f"_{kw}")
        ):
            return True
    return False


_LONG_FLAG_KEYWORDS: tuple[str, ...] = (
    "password",
    "pass",
    "token",
    "secret",
    "key",
    "credential",
)

# Character set used by the token-wise URL scanner below
# (``_redact_url_basic_auth``). Defect A (panel iteration 3, verified by
# executing code): a THIRD implementation of these two rules — a
# whole-text positional scanner — replaced the length-bounded regexes
# from iteration 2. It was fast and, for the URL rule, unbounded in
# theory, but it carried a shape bug of its own:
#
#   * ``_redact_long_flags`` (the old whole-text version) assumed that
#     for a space-separated flag the NEXT token was always that flag's
#     value, and jumped its scan position straight past it
#     (``i = value_end``). For a routine command shape — a boolean flag
#     immediately followed by a credential flag, e.g.
#     ``docker run --rm --password=Secret99`` or
#     ``cmd --verbose --password=Secret123`` — that jump landed
#     ``i`` in the middle of ``--password=Secret99``'s own token, so it
#     was never classified at all. Not a rare edge case: ``--rm``,
#     ``--verbose``, ``--detach``, ``--help``, ``--dry-run`` are all
#     ordinary boolean flags.
#   * ``_redact_url_basic_auth`` (the old whole-text version) capped the
#     backward scheme walk at 32 chars for O(1)-per-candidate lookup.
#     Real schemes are short, but a scheme *shape* longer than 32 chars
#     (``a`` + 40 digits + ``://user:Secret123@host``) walked back only
#     32 chars, landed on a digit instead of the leading letter, was
#     judged "not alpha-led", and the entire URL — including its
#     password — was skipped rather than redacted.
#
# Adding a fourth special case for each of these would only fix the
# instance, not the class (this is the third time that has happened).
# The fix is a shape change: tokenize the command ONCE, in original
# order, with exact whitespace preserved (``_split_ws_preserving``,
# Unicode-aware via ``str.isspace()`` rather than a hardcoded
# ``" \t\r\n"`` set — a hardcoded set is exactly what silently narrowed
# the old regex's ``\s+`` and would let NBSP/vertical-tab-separated
# flags slip past unclassified), then classify EVERY non-whitespace
# token on its own turn in ``_redact_long_flags`` and
# ``_redact_url_basic_auth``. A flag may look exactly one token ahead to
# consume a value, but nothing can cause a LATER token to be skipped
# without first being classified — that is what makes "skip a
# candidate" impossible rather than merely unobserved so far. Both
# scanners stay O(n): tokenizing is one linear pass, and every rule
# after that only ever looks at TOKEN-local text (a token has no
# internal whitespace by construction), so there is no unbounded
# quantifier to bound in the first place — which is also what makes the
# URL scheme walk safe to leave unbounded (see ``_redact_url_in_token``).
_URL_TERMINATORS: frozenset[str] = frozenset(":@/?#")


def _split_ws_preserving(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into alternating ``(chunk, is_whitespace)`` runs, in
    original order, that reassemble byte-identical via
    ``"".join(chunk for chunk, _ in result)``.

    Uses ``str.isspace()`` (Unicode-aware) rather than a fixed character
    set — see the module comment above ``_URL_TERMINATORS`` for why a
    hardcoded set is itself a leak vector here.
    """
    n = len(text)
    if n == 0:
        return []
    chunks: list[tuple[str, bool]] = []
    start = 0
    cur_ws = text[0].isspace()
    for i in range(1, n):
        c_ws = text[i].isspace()
        if c_ws != cur_ws:
            chunks.append((text[start:i], cur_ws))
            start = i
            cur_ws = c_ws
    chunks.append((text[start:n], cur_ws))
    return chunks


def _redact_url_in_token(token: str) -> str:
    """Redact ``scheme://user:password@host`` within a single
    whitespace-delimited token. O(len(token)); scheme, userinfo, and
    password are all unbounded — see the module comment above
    ``_URL_TERMINATORS``.

    Precomputing "nearest terminator" and "nearest '@'" once, in a
    single reverse pass, keeps each ``"://"`` candidate's forward lookups
    O(1); the backward scheme walk has no precomputed bound; it is safe
    to leave unbounded because it is scoped to one token and the ``'/'``
    inside ``"://"`` itself stops the NEXT candidate's walk immediately,
    so no span of the token is ever walked twice.
    """
    n = len(token)
    if "://" not in token:
        return token

    next_terminator = [n] * (n + 1)
    next_at = [n] * (n + 1)
    t_term = t_at = n
    for idx in range(n - 1, -1, -1):
        c = token[idx]
        if c in _URL_TERMINATORS:
            t_term = idx
        if c == "@":
            t_at = idx
        next_terminator[idx] = t_term
        next_at[idx] = t_at

    out: list[str] = []
    last_emit = 0
    i = 0
    while True:
        idx = token.find("://", i)
        if idx == -1:
            break

        j = idx
        while j > 0 and (token[j - 1].isalnum() or token[j - 1] in "+.-"):
            j -= 1
        if j == idx or not token[j].isalpha():
            i = idx + 3
            continue

        p = idx + 3
        term = next_terminator[p] if p <= n else n
        if term >= n or token[term] != ":":
            i = idx + 3
            continue
        colon_pos = term

        pass_start = colon_pos + 1
        at_pos = next_at[pass_start] if pass_start <= n else n
        if at_pos >= n:
            i = idx + 3
            continue

        out.append(token[last_emit : colon_pos + 1])
        out.append(_REDACTION_PLACEHOLDER)
        out.append("@")
        last_emit = at_pos + 1
        i = at_pos + 1

    out.append(token[last_emit:])
    return "".join(out)


def _redact_url_basic_auth(text: str) -> str:
    """Token-wise redaction of ``scheme://user:password@host``.

    Replaces the old whole-text positional scanner. See the module
    comment above ``_URL_TERMINATORS`` for the specific leak
    (32-char-capped scheme walk) this closes.
    """
    if "://" not in text:
        return text
    chunks = _split_ws_preserving(text)
    return "".join(
        chunk if is_ws or "://" not in chunk else _redact_url_in_token(chunk)
        for chunk, is_ws in chunks
    )


# Matches a ``--<name>=`` flag shape anywhere within a single token (not
# anchored to the token start) — see ``_redact_credential_in_token``.
_LONG_FLAG_EQ_RE: re.Pattern[str] = re.compile(r"--[\w-]+=")


def _redact_credential_in_token(token: str) -> str:
    """Redact the first credential-shaped ``--<name>=`` occurring
    ANYWHERE within ``token`` — not only at offset 0 — from that name's
    ``=`` through the end of the token.

    Defect 1 (panel iteration 4, cursor, verified by executing code and
    diffing against main): the original whole-text ``re.sub`` scanned
    every position in the command string, so a credential flag nested
    inside another token's value — ``--flag=--password=secret`` — was
    still found and redacted (the match starts wherever ``--password=``
    begins, regardless of what precedes it). The token-wise rewrite
    (Defect A) that replaced it classified only the flag NAME at a
    token's start (before the first ``=``), so ``--flag`` (not
    credential-shaped) short-circuited the whole token and the nested
    ``--password=secret`` was never looked at — regressing a case the
    original handled. ``finditer`` restores the "scan every position"
    property within THIS token: it walks left to right past each
    non-credential ``--name=`` candidate (e.g. ``--flag=``) until it
    finds one that is credential-shaped, then redacts from there. This
    keeps the token-wise property that made Defect A's fix work — no
    TOKEN can be skipped, because the caller below still visits every
    token on its own turn — while no longer stopping at the first ``=``
    within a token.
    """
    for match in _LONG_FLAG_EQ_RE.finditer(token):
        name = match.group()[2:-1]  # strip leading "--" and trailing "="
        if _long_flag_is_credential(name):
            return token[: match.end()] + _placeholder_for(token[match.end() :])
    return token


def _redact_long_flags(text: str) -> str:
    """Token-wise redaction of ``--<flag>=<value>`` / ``--<flag> <value>``.

    Replaces the old whole-text positional scanner. See the module
    comment above ``_URL_TERMINATORS`` for the specific leak (skipping a
    credential flag that immediately follows a boolean flag) this
    closes. Every non-whitespace token is classified on its own loop
    turn:

    * ``--<name>=<value>`` — a credential-shaped ``--<name>=`` occurring
      ANYWHERE in the token (see ``_redact_credential_in_token``, Defect
      1) redacts from that ``=`` to the end of THIS token.
    * ``--<name>`` alone — credential-shaped ``<name>`` looks at exactly
      the next non-whitespace token and redacts it, UNLESS that token
      itself starts with ``--`` (then this flag was boolean/valueless;
      the next token is left untouched here and gets its own turn on
      the next loop iteration — it is never silently skipped).
    * anything else — passed through unchanged.

    Defect (found 2026-07-25, reported against ``main``): the guard
    above used to test for a single leading ``-``, not ``--``. A
    credential VALUE that itself begins with ``-`` (e.g. a numeric
    argument like ``-0000000``, or any dash-led token) is
    indistinguishable from a flag under a single-dash test, so it was
    passed through unredacted — ``mysql --password -0000000 somedb``
    leaked the password verbatim. Narrowing the guard to ``--`` fixes
    this without reopening the original skip-leak it exists to
    prevent: a ``--``-prefixed token is still almost certainly a long
    flag, AND it still gets its own classification turn on the next
    loop iteration (so ``--password --token=x`` still redacts
    ``--token=x`` — no skip). A single-dash token (``-p``, ``-rf``,
    bare ``-``, or ``-0000000``) is now treated as the credential's
    value and redacted; for a tripwire, over-redaction is strictly
    safer than under-redaction (see the module docstring above
    ``_REDACTION_PLACEHOLDER``).

    Note (Defect 1 follow-up, scope note): a credential value that is
    itself shell-quoted and contains whitespace (``--password="a b"``)
    is only partially redacted here, same as on ``main`` — matching
    ``\\S+``-style value matching is inherently quote-unaware. This
    module is a TRIPWIRE, not a proof of correctness (see the module
    docstring above ``_REDACTION_PLACEHOLDER``); the documented
    mitigation is to pass credentials via env files or stdin rather than
    argv, not to widen this scanner into a shell-quoting parser.
    """
    if "--" not in text:
        return text
    chunks = _split_ws_preserving(text)
    out = [chunk for chunk, _is_ws in chunks]
    n = len(chunks)
    i = 0
    while i < n:
        chunk, is_ws = chunks[i]
        if is_ws or not chunk.startswith("--"):
            i += 1
            continue

        flag_name, eq, _value = chunk.partition("=")
        if eq:
            out[i] = _redact_credential_in_token(chunk)
            i += 1
            continue

        if _long_flag_is_credential(flag_name):
            j = i + 1
            if j < n and chunks[j][1]:  # skip exactly one whitespace run
                j += 1
            # Guard narrowed from "-" to "--" (defect above): a
            # dash-prefixed VALUE (e.g. "-0000000") must still be
            # redacted; only a genuine "--"-prefixed long flag is
            # treated as this flag being boolean/valueless.
            if j < n and not chunks[j][1] and not chunks[j][0].startswith("--"):
                out[j] = _placeholder_for(chunks[j][0])
                i = j + 1
                continue
        i += 1

    return "".join(out)


def _build_credential_subs() -> list[Callable[[str], str]]:
    """Compile the ordered redaction pipeline.

    Each entry is a single-argument callable that returns the redacted
    string. Order matters: URL basic-auth runs first so the ``@``
    separator can't be swallowed by a later rule. Env-var patterns run
    before short-flag patterns because ``PGPASSWORD=xxx`` is more
    specific than any flag-based match, and the two hand-written scanners
    (Defect A) that replaced the old regex rules 1 and 8/9 keep their
    original relative position — URL first, long-flags last.

    Rules 4/5/6 use lookbehind, which is why this pipeline stays on
    Python's ``re`` engine for everything except the two rules that
    needed hand-written scanners (RE2 was considered and rejected for
    that reason).
    """
    env_alt = "|".join(re.escape(name) for name in _SECRET_ENV_NAMES)

    def _regex_step(pattern: re.Pattern[str], repl: Any) -> Callable[[str], str]:
        return lambda text: pattern.sub(repl, text)

    return [
        # 1. Basic auth credentials embedded in a URL:
        #    ``scheme://user:password@host``. See _redact_url_basic_auth.
        #
        # Keeps the PLAIN placeholder: the replacement is bounded on both
        # sides (``:`` … ``@``), so nothing beyond the secret is consumed
        # and there is nothing to flag. Same for rule 4 below, whose value
        # is delimited by its closing quote. Every OTHER rule here
        # replaces to the end of a whitespace-delimited run and therefore
        # goes through ``_placeholder_for`` — a rule that swallows
        # ``;reboot`` must say so. Reported by CI's 200-example Hypothesis
        # profile 2026-09-07: `--…-password=x&` was flagged while
        # `--…-token=x&` was not, because rule 3a's enumerated env names
        # matched the second one first. An inconsistent marker is worse
        # than none, since a reader would take its absence as proof that
        # nothing was cut.
        _redact_url_basic_auth,
        # 2. HTTP ``Authorization:`` header with Bearer/Basic/Digest/Token.
        _regex_step(
            re.compile(
                r"(Authorization:\s*(?:Bearer|Basic|Digest|Token)\s+)(\S+)",
                re.IGNORECASE,
            ),
            lambda m: f"{m.group(1)}{_placeholder_for(m.group(2))}",
        ),
        # 3a. Known credential env vars (enumerated list, exact match).
        _regex_step(
            re.compile(
                r"\b(" + env_alt + r")=(\S+)",
                re.IGNORECASE,
            ),
            lambda m: f"{m.group(1)}={_placeholder_for(m.group(2))}",
        ),
        # 3b. (v0.4.3 G2) Generic env var SUFFIX patterns:
        #     ``*_PASSWORD=``, ``*_SECRET=``, ``*_TOKEN=``, ``*_KEY=``,
        #     ``*_CREDENTIAL=``, ``*_PWD=``. Catches ``VAULT_TOKEN``,
        #     ``STRIPE_SECRET_KEY``, ``MY_CUSTOM_PASSWORD``, etc. without
        #     needing to enumerate every possible prefix. Safe unbounded:
        #     the leading ``\b`` anchors the number of match ATTEMPTS to
        #     the number of word-boundary starts, not every character
        #     position, so a long run with no ``=`` costs O(run length)
        #     once, not once per position within it.
        _regex_step(
            re.compile(
                r"\b(\w+(?:_PASSWORD|_SECRET|_TOKEN|_KEY|_CREDENTIAL|_PWD))=(\S+)",
                re.IGNORECASE,
            ),
            lambda m: f"{m.group(1)}={_placeholder_for(m.group(2))}",
        ),
        # 4. MySQL/MariaDB short password flag QUOTED form. Plain
        #    placeholder: the closing quote bounds the replacement.
        _regex_step(
            re.compile(r"(?<![\w-])(-p)(['\"])([^'\"]*)(\2)"),
            lambda m: f"{m.group(1)}{m.group(2)}{_REDACTION_PLACEHOLDER}{m.group(4)}",
        ),
        # 5. MySQL/MariaDB short password flag UNQUOTED form (≥3 chars).
        _regex_step(
            re.compile(r"(?<![\w-])(-p)(\S{3,})"),
            lambda m: f"{m.group(1)}{_placeholder_for(m.group(2))}",
        ),
        # 6. (v0.4.3 G4) ``curl -u user:password`` basic auth flag.
        #    Redacts the password portion after the colon.
        _regex_step(
            re.compile(r"(?<!\w)(-u\s+\S+:)(\S+)"),
            lambda m: f"{m.group(1)}{_placeholder_for(m.group(2))}",
        ),
        # 7. (v0.4.3 G4) ``sshpass -p PASSWORD`` (space-separated).
        #    sshpass uses ``-p`` with a SPACE before the password, unlike
        #    MySQL which uses no space. Match ``sshpass`` prefix to
        #    disambiguate from the MySQL rule.
        _regex_step(
            re.compile(r"(sshpass\s+-p\s+)(\S+)", re.IGNORECASE),
            lambda m: f"{m.group(1)}{_placeholder_for(m.group(2))}",
        ),
        # 8/9. Long flags with ``=`` or whitespace separator. See
        #      _redact_long_flags — merges the old rules 8 and 9 into one
        #      scanner since both forms share the same flag-name/keyword
        #      logic.
        _redact_long_flags,
    ]


_CREDENTIAL_SUBS: list[Callable[[str], str]] = _build_credential_subs()


def _redact_secrets(value: Any) -> Any:
    """Replace known credential patterns in ``value`` with a placeholder.

    Runs a targeted pipeline of substitutions for the most common
    command-line credential leak patterns (``-pSecret``,
    ``--password=xxx``, ``PGPASSWORD=xxx``, ``Authorization: Bearer xxx``,
    ``https://user:pass@host``, and the other patterns compiled by
    ``_build_credential_subs``).

    Idempotent: applying the function to already-redacted text produces
    identical output — the placeholder itself does not match any rule.
    Safe for ``None`` and empty strings (passed through unchanged).
    Non-string inputs are returned as-is so structured values (ints,
    dicts, etc.) are not accidentally coerced by the caller.

    This is a TRIPWIRE, not a proof of correctness — operators must
    still prefer env-file secrets or stdin over command-line arguments.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return value
    out = value
    for step in _CREDENTIAL_SUBS:
        out = step(out)
    return out
