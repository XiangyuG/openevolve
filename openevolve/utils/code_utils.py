"""
Utilities for code parsing, diffing, and manipulation
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union


def parse_evolve_blocks(code: str) -> List[Tuple[int, int, str]]:
    """
    Parse evolve blocks from code

    Args:
        code: Source code with evolve blocks

    Returns:
        List of tuples (start_line, end_line, block_content)
    """
    lines = code.split("\n")
    blocks = []

    in_block = False
    start_line = -1
    block_content = []

    for i, line in enumerate(lines):
        if "# EVOLVE-BLOCK-START" in line:
            in_block = True
            start_line = i
            block_content = []
        elif "# EVOLVE-BLOCK-END" in line and in_block:
            in_block = False
            blocks.append((start_line, i, "\n".join(block_content)))
        elif in_block:
            block_content.append(line)

    return blocks


def apply_diff(
    original_code: str,
    diff_text: str,
    diff_pattern: str = r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE",
) -> str:
    """
    Apply a diff to the original code

    Args:
        original_code: Original source code
        diff_text: Diff in the SEARCH/REPLACE format
        diff_pattern: Regex pattern for the SEARCH/REPLACE format

    Returns:
        Modified code
    """
    # Split into lines for easier processing
    original_lines = original_code.split("\n")
    result_lines = original_lines.copy()

    # Extract diff blocks
    diff_blocks = extract_diffs(diff_text, diff_pattern)

    # Apply each diff block
    for search_text, replace_text in diff_blocks:
        search_lines = search_text.split("\n")
        replace_lines = replace_text.split("\n")

        # Find where the search pattern starts in the original code
        for i in range(len(result_lines) - len(search_lines) + 1):
            if result_lines[i : i + len(search_lines)] == search_lines:
                # Replace the matched section
                result_lines[i : i + len(search_lines)] = replace_lines
                break

    return "\n".join(result_lines)


def extract_diffs(
    diff_text: str, diff_pattern: str = r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE"
) -> List[Tuple[str, str]]:
    """
    Extract diff blocks from the diff text

    Args:
        diff_text: Diff in the SEARCH/REPLACE format
        diff_pattern: Regex pattern for the SEARCH/REPLACE format

    Returns:
        List of tuples (search_text, replace_text)
    """
    diff_blocks = re.findall(diff_pattern, diff_text, re.DOTALL)
    return [(match[0].rstrip(), match[1].rstrip()) for match in diff_blocks]


def parse_full_rewrite(llm_response: str, language: str = "python") -> Optional[str]:
    """
    Extract a full rewrite from an LLM response

    Args:
        llm_response: Response from the LLM
        language: Programming language

    Returns:
        Extracted code or None if not found
    """
    code_block_pattern = r"```" + language + r"\n(.*?)```"
    matches = re.findall(code_block_pattern, llm_response, re.DOTALL)

    if matches:
        return matches[0].strip()

    # Fallback to any code block
    code_block_pattern = r"```(.*?)```"
    matches = re.findall(code_block_pattern, llm_response, re.DOTALL)

    if matches:
        return matches[0].strip()

    # No closed code block found. If the response never used a fence at all,
    # treat the whole response as code -- some models return bare code with
    # no markdown wrapping, and that's a legitimate response shape. But if it
    # DID open a fence and never closed it (truncated/malformed response),
    # that's not "the whole reply is code" -- returning it verbatim would
    # silently glue prose onto the program (and try to compile/run that)
    # instead of failing clearly with "no valid code found".
    if "```" not in llm_response:
        return llm_response
    return None


def extract_change_explanation(
    llm_response: str,
    diff_pattern: str = r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE",
) -> str:
    """
    Pull out the LLM's natural-language explanation of its changes from its raw
    response, by stripping out the SEARCH/REPLACE diff blocks (or fenced code
    blocks, for full rewrites) and returning whatever text is left.

    Args:
        llm_response: Raw text returned by the LLM
        diff_pattern: Regex pattern used to find SEARCH/REPLACE diff blocks

    Returns:
        Remaining explanation text (stripped), or "" if nothing is left
    """
    remaining = re.sub(diff_pattern, "", llm_response, flags=re.DOTALL)
    remaining = re.sub(r"```.*?```", "", remaining, flags=re.DOTALL)
    remaining = re.sub(r"\n{3,}", "\n\n", remaining)
    return remaining.strip()


_WITNESS_BLOCK_PATTERN = re.compile(
    r"^\s*\(\d+\)\s*(?P<summary>.*?)\s*\n"
    r"(?P<detail>.*?)"
    r"\s*Witness:\s*(?P<witness>.*?)\s*\n"
    r"\s*Formula \(pre-transformation\):\s*(?P<pre_formula>.*?)\s*\n"
    r"\s*Formula \(post-transformation\):\s*(?P<post_formula>.*?)"
    r"(?:\s*\n\s*Map value width change:\s*(?P<map_width_change>[^\n]*))?"
    r"(?:\s*\n\s*Variable width change:\s*(?P<variable_width_change>[^\n]*))?"
    r"(?:\s*\n\s*Map fusion:\s*(?P<map_fusion>[^\n]*))?"
    r"\s*(?=\n\s*\(\d+\)\s|\Z)",
    re.MULTILINE | re.DOTALL,
)

_TRAILING_ELLIPSIS_PATTERN = re.compile(r"\n?\.\.\.\s*$")

_EXAMPLE_SNIPPET_PATTERN = re.compile(r"`[^`\n]+`")


def _has_example_snippet(detail: str) -> bool:
    """Whether `detail` (the free-form text between the numbered summary and
    "Witness:", nominally an "Example: `<old>` -> `<new>`" line) contains at
    least one non-empty single-backtick-quoted code fragment. Models sometimes
    emit the "Example:"/"Old:"/"New:" label(s) with nothing after them (seen in
    practice: "Old: \\n\\n    New:\\n" with no code at all) -- extract_transformation_witnesses
    doesn't require any particular label here (see its docstring), so that case
    would otherwise pass through silently as an empty-looking detail string
    instead of being flagged for the reviewer."""
    return bool(_EXAMPLE_SNIPPET_PATTERN.search(detail))

_MAP_WIDTH_CHANGE_PATTERN = re.compile(r"^(?P<map>[^:]+):\s*(?P<old>\d+)\s*->\s*(?P<new>\d+)\s*$")

_VARIABLE_WIDTH_CHANGE_PATTERN = re.compile(r"^(?P<var>[^:]+):\s*(?P<old>\d+)\s*->\s*(?P<new>\d+)\s*$")


def _parse_map_width_change(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse an optional "Map value width change: <map>: <old> -> <new>" tag
    (see examples/bpf_compile/prompt_templates) into a structured hint, or
    None if absent/malformed. This is only a HINT of which map + byte sizes a
    developer-approved witness claims to narrow -- the actual sizes must be
    cross-checked against real BTF metadata downstream (e.g. by the semantic
    checker itself) before being trusted, since the LLM's stated numbers could
    be wrong even when the surrounding formula is sound."""
    if not raw:
        return None
    match = _MAP_WIDTH_CHANGE_PATTERN.match(raw.strip())
    if not match:
        return None
    return {
        "map": match.group("map").strip(),
        "old_bytes": int(match.group("old")),
        "new_bytes": int(match.group("new")),
    }


def _parse_variable_width_change(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse an optional "Variable width change: <var>: <old_bits> -> <new_bits>"
    tag (see examples/bpf_compile/prompt_templates) into a structured hint, or
    None if absent/malformed. The value-range invariant that justifies the
    narrowing is expected to live in the witness's own pre-transformation
    Formula (as an assumption on the shared input constant, checked by Z3
    alongside pre_result/post_result equivalence) -- this tag only carries
    which variable + bit widths the witness claims to narrow. Like
    _parse_map_width_change, it is only a HINT: the claimed bit widths must
    still be cross-checked downstream (e.g. by the semantic checker's own
    range analysis) before being trusted, since the LLM's stated numbers could
    be wrong even when the surrounding formula is sound."""
    if not raw:
        return None
    match = _VARIABLE_WIDTH_CHANGE_PATTERN.match(raw.strip())
    if not match:
        return None
    return {
        "var": match.group("var").strip(),
        "old_bits": int(match.group("old")),
        "new_bits": int(match.group("new")),
    }


_MAP_FUSION_PATTERN = re.compile(r"^(?P<target>[^=]+?)\s*=\s*(?P<sources>.+)$")

_MAP_FUSION_SOURCE_PATTERN = re.compile(
    r"^(?P<map>[^@]+?)@(?P<offset>\d+):(?P<width>\d+)$"
)


def _parse_map_fusion(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse an optional "Map fusion: <target> = <src1>@<offset1>:<width1> +
    <src2>@<offset2>:<width2> [+ ...]" tag (see
    examples/bpf_compile/prompt_templates) into a structured hint, or None if
    absent/malformed. Mirrors _parse_map_width_change: this is only a HINT of
    which source maps the witness claims were folded into which target map,
    and at what byte offset/width each source's data now lives -- the actual
    layout must still be cross-checked downstream (heimdall-private's
    verify_equivalence.py, via this hint's "map_fusion" shape) before being
    trusted."""
    if not raw:
        return None
    match = _MAP_FUSION_PATTERN.match(raw.strip())
    if not match:
        return None
    target = match.group("target").strip()
    sources = []
    for part in match.group("sources").split("+"):
        source_match = _MAP_FUSION_SOURCE_PATTERN.match(part.strip())
        if not source_match:
            return None
        sources.append(
            {
                "map": source_match.group("map").strip(),
                "value_offset_bytes": int(source_match.group("offset")),
                "value_bytes": int(source_match.group("width")),
            }
        )
    if not target or not sources:
        return None
    return {"target": target, "sources": sources}


def extract_transformation_witnesses(explanation_text: str) -> List[Dict[str, Any]]:
    """
    Pull out per-change "Witness"/"Formula (pre/post-transformation)" entries
    from an LLM explanation that follows the (1)/(2)/... numbered format (see
    examples/bpf_compile/prompt_templates). Only "Witness:" and the two
    "Formula (...):" labels are required verbatim -- whatever's between the
    numbered summary and "Witness:" (nominally an "Example:" line) is captured
    as-is in "detail", since models don't always use that exact label (e.g.
    "Old:"/"New:" pairs instead), and requiring it verbatim silently dropped
    the whole item, formulas included. Entries missing any of these lines are
    simply not matched -- this is best-effort, not enforced.

    Args:
        explanation_text: Explanation text, e.g. from extract_change_explanation()

    Returns:
        List of {"summary", "detail", "witness", "pre_formula", "post_formula",
        "map_width_change", "variable_width_change", "map_fusion",
        "example_missing"} dicts, in order.
        "map_width_change" is {"map", "old_bytes", "new_bytes"} or None when
        the LLM didn't tag this witness as a map value-width change.
        "variable_width_change" is {"var", "old_bits", "new_bits"} or None
        when the LLM didn't tag this witness as a range-justified variable
        narrowing.
        "map_fusion" is {"target", "sources": [{"map", "value_offset_bytes",
        "value_bytes"}, ...]} or None when the LLM didn't tag this witness as
        a map merge.
        "example_missing" is True when "detail" has no non-empty
        backtick-quoted code fragment -- i.e. the LLM skipped quoting the
        actual before/after code the prompt asks for (see
        _has_example_snippet).
    """
    witnesses = []
    for match in _WITNESS_BLOCK_PATTERN.finditer(explanation_text):
        # The prompt's own "..." continuation marker sometimes gets echoed back
        # by the model as trailing filler after the last real item; drop it.
        pre_formula = _TRAILING_ELLIPSIS_PATTERN.sub("", match.group("pre_formula").strip())
        post_formula = _TRAILING_ELLIPSIS_PATTERN.sub("", match.group("post_formula").strip())
        detail = match.group("detail").strip()
        witnesses.append(
            {
                "summary": match.group("summary").strip(),
                "detail": detail,
                "witness": match.group("witness").strip(),
                "pre_formula": pre_formula.strip(),
                "post_formula": post_formula.strip(),
                "map_width_change": _parse_map_width_change(match.group("map_width_change")),
                "variable_width_change": _parse_variable_width_change(
                    match.group("variable_width_change")
                ),
                "map_fusion": _parse_map_fusion(match.group("map_fusion")),
                "example_missing": not _has_example_snippet(detail),
            }
        )
    return witnesses


def _find_const_by_name(assertions, name: str):
    """Walk a z3 AstVector of assertions looking for a 0-arity application
    (a declared constant) with the given name, returning its actual AST node
    (with its real sort) -- NOT a freshly-constructed z3.Int/z3.Bool, since
    guessing the sort and constructing a same-named constant would silently
    create an unrelated symbol instead of erroring on a sort mismatch."""
    seen = set()

    def walk(expr):
        # Dedup by z3's own AST id (expr.get_id()), NOT Python's id(expr):
        # z3py hands back a fresh wrapper object on every .arg(i) call, so
        # Python object ids get freed and immediately recycled for unrelated
        # sibling nodes -- keying "seen" on those caused real, silent misses
        # (a later assert's reference to an already-visited-looking id got
        # skipped) once formulas had more than one top-level assert, e.g. a
        # range hypothesis asserted before the pre_result/post_result
        # definition.
        ast_id = expr.get_id()
        if ast_id in seen:
            return None
        seen.add(ast_id)
        if expr.num_args() == 0 and expr.decl().name() == name:
            return expr
        for i in range(expr.num_args()):
            found = walk(expr.arg(i))
            if found is not None:
                return found
        return None

    for a in assertions:
        found = walk(a)
        if found is not None:
            return found
    return None


def validate_transformation_proof(pre_formula: str, post_formula: str) -> Dict[str, Optional[str]]:
    """
    Check whether a witness's pre/post-transformation formulas actually PROVE
    the change preserves semantics, rather than just checking they parse.

    Each formula is expected to be a self-contained SMT-LIB 2 script that
    declares a constant named `pre_result` (respectively `post_result`) and
    asserts what it equals. Both are parsed, then combined with
    `(assert (not (= pre_result post_result)))`: UNSAT means pre_result and
    post_result are provably equal for every input (the change is proven
    equivalent for the modeled behavior); SAT means z3 found a concrete
    counterexample where they'd differ -- a real, checkable divergence, not
    just an LLM claim. z3-solver is an optional dependency of this check, not
    of OpenEvolve itself.

    Args:
        pre_formula: SMT-LIB 2 snippet defining pre_result
        post_formula: SMT-LIB 2 snippet defining post_result

    Returns:
        {"status": "trivial"|"proven_equivalent"|"counterexample_found"|
                    "unknown"|"parse_error"|"malformed"|"unavailable",
         "detail": human-readable explanation or None}
    """
    try:
        import z3
    except ImportError:
        return {"status": "unavailable", "detail": "z3-solver not installed"}

    if pre_formula.strip() == "(assert true)" and post_formula.strip() == "(assert true)":
        return {"status": "trivial", "detail": "structural change, no proof needed"}

    try:
        pre_assertions = z3.parse_smt2_string(pre_formula)
    except z3.Z3Exception as e:
        return {"status": "parse_error", "detail": f"pre-transformation: {e}"}

    try:
        post_assertions = z3.parse_smt2_string(post_formula)
    except z3.Z3Exception as e:
        return {"status": "parse_error", "detail": f"post-transformation: {e}"}

    pre_result = _find_const_by_name(pre_assertions, "pre_result")
    if pre_result is None:
        return {"status": "malformed", "detail": "pre-transformation formula does not define pre_result"}

    post_result = _find_const_by_name(post_assertions, "post_result")
    if post_result is None:
        return {"status": "malformed", "detail": "post-transformation formula does not define post_result"}

    try:
        solver = z3.Solver()
        solver.add(pre_assertions)
        solver.add(post_assertions)
        solver.add(pre_result != post_result)
        result = solver.check()
    except z3.Z3Exception as e:
        return {"status": "malformed", "detail": f"pre_result/post_result sort mismatch: {e}"}
    except Exception as e:  # pragma: no cover - defensive, z3 check() rarely throws
        return {"status": "unknown", "detail": str(e)}

    if result == z3.unsat:
        return {"status": "proven_equivalent", "detail": None}
    if result == z3.sat:
        return {"status": "counterexample_found", "detail": str(solver.model())}
    return {"status": "unknown", "detail": "solver returned unknown"}


def format_witness_decisions_for_prompt(witnesses: List[Dict[str, Any]]) -> str:
    """
    Render a phase-1 (proposal) witness list tagged with the developer's per-witness
    approve/reject call, for use as the "Approved Changes To Implement" section of the
    phase-2 (implement) prompt (see examples/bpf_compile/prompt_templates/*_implement.txt).
    Witnesses without an explicit `developer_approved is True` (i.e. rejected or never
    reviewed) are tagged as not-to-implement rather than omitted, so the LLM sees the full
    original proposal and doesn't reintroduce a rejected change under a different guise.

    Args:
        witnesses: Witness dicts as produced by extract_transformation_witnesses, each
            stamped with a "developer_approved" key (True/False/None)

    Returns:
        Formatted text, one block per witness (empty string if no witnesses)
    """
    blocks = []
    for w in witnesses:
        tag = (
            "APPROVED -- implement this exactly as described"
            if w.get("developer_approved") is True
            else "REJECTED/NOT REVIEWED -- do NOT implement this"
        )
        lines = [f"({w.get('index', '?')}) {w.get('summary', '')} [{tag}]"]
        if w.get("detail"):
            lines.append(f"    {w['detail']}")
        if w.get("witness"):
            lines.append(f"    Witness: {w['witness']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_heimdall_witness_file(witnesses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Combine extracted witnesses (see extract_transformation_witnesses) into
    heimdall-private's --witness-file JSON schema (c2rust_translation/
    verify_equivalence.py's load_witnesses/relax_hints_from_witnesses/
    fusion_hints_from_witnesses): {"witnesses": [{"id", "map_width_change"} or
    {"id", "map_fusion"}, ...]}.

    Only witnesses carrying a "map_width_change" or "map_fusion" hint are
    included -- heimdall only ever reads those two keys off each entry, so a
    purely structural witness (or one with only a "variable_width_change" tag,
    which heimdall has no map-level model for) would contribute nothing and is
    left out. "id" is the witness's own "index" (see process_parallel.py's
    _run_iteration_worker_propose), so ids in the resulting file line up with
    the witness numbering shown in the review UI.

    Args:
        witnesses: Witness dicts, each stamped with an "index"

    Returns:
        {"witnesses": [...]} dict, ready to json.dump to a --witness-file path
        ("witnesses": [] if none qualify)
    """
    entries = []
    for w in witnesses:
        if w.get("map_width_change"):
            entries.append({"id": w.get("index"), "map_width_change": w["map_width_change"]})
        elif w.get("map_fusion"):
            entries.append({"id": w.get("index"), "map_fusion": w["map_fusion"]})
    return {"witnesses": entries}


def _format_block_lines(lines: List[str], max_line_len: int = 100, max_lines: int = 30) -> str:
    """Format a block of lines for diff summary: show all lines (truncated per line, optional cap)."""
    truncated = []
    for line in lines[:max_lines]:
        s = line.rstrip()
        if len(s) > max_line_len:
            s = s[: max_line_len - 3] + "..."
        truncated.append("  " + s)
    if len(lines) > max_lines:
        truncated.append(f"  ... ({len(lines) - max_lines} more lines)")
    return "\n".join(truncated) if truncated else "  (empty)"


def format_diff_summary(
    diff_blocks: List[Tuple[str, str]],
    max_line_len: int = 100,
    max_lines: int = 30,
) -> str:
    """
    Create a human-readable summary of the diff.
    For multi-line blocks, shows the full search and replace content (all lines).

    Args:
        diff_blocks: List of (search_text, replace_text) tuples
        max_line_len: Maximum characters per line before truncation (default: 100)
        max_lines: Maximum lines per SEARCH/REPLACE block (default: 30)

    Returns:
        Summary string
    """
    summary = []

    for i, (search_text, replace_text) in enumerate(diff_blocks):
        search_lines = search_text.strip().split("\n")
        replace_lines = replace_text.strip().split("\n")

        if len(search_lines) == 1 and len(replace_lines) == 1:
            summary.append(f"Change {i+1}: '{search_lines[0]}' to '{replace_lines[0]}'")
        else:
            search_block = _format_block_lines(search_lines, max_line_len, max_lines)
            replace_block = _format_block_lines(replace_lines, max_line_len, max_lines)
            summary.append(f"Change {i+1}: Replace:\n{search_block}\nwith:\n{replace_block}")

    return "\n".join(summary)


def calculate_edit_distance(code1: str, code2: str) -> int:
    """
    Calculate the Levenshtein edit distance between two code snippets

    Args:
        code1: First code snippet
        code2: Second code snippet

    Returns:
        Edit distance (number of operations needed to transform code1 into code2)
    """
    if code1 == code2:
        return 0

    # Simple implementation of Levenshtein distance
    m, n = len(code1), len(code2)
    dp = [[0 for _ in range(n + 1)] for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i

    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if code1[i - 1] == code2[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,  # deletion
                dp[i][j - 1] + 1,  # insertion
                dp[i - 1][j - 1] + cost,  # substitution
            )

    return dp[m][n]


def extract_code_language(code: str) -> str:
    """
    Try to determine the language of a code snippet

    Args:
        code: Code snippet

    Returns:
        Detected language or "unknown"
    """
    # Look for common language signatures
    if re.search(r"^(import|from|def|class)\s", code, re.MULTILINE):
        return "python"
    elif re.search(r"^(package|import java|public class)", code, re.MULTILINE):
        return "java"
    elif re.search(r"^(#include|int main|void main)", code, re.MULTILINE):
        return "cpp"
    elif re.search(r"^(function|var|let|const|console\.log)", code, re.MULTILINE):
        return "javascript"
    elif re.search(r"^(module|fn|let mut|impl)", code, re.MULTILINE):
        return "rust"
    elif re.search(r"^(SELECT|CREATE TABLE|INSERT INTO)", code, re.MULTILINE):
        return "sql"

    return "unknown"


def _can_apply_linewise(haystack_lines: List[str], needle_lines: List[str]) -> bool:
    if not needle_lines:
        return False

    for i in range(len(haystack_lines) - len(needle_lines) + 1):
        if haystack_lines[i : i + len(needle_lines)] == needle_lines:
            return True

    return False


def apply_diff_blocks(original_text: str, diff_blocks: List[Tuple[str, str]]) -> Tuple[str, int]:
    """
    Apply diff blocks line-wise and return (new_text, applied_count)
    """
    lines = original_text.split("\n")
    applied = 0

    for search_text, replace_text in diff_blocks:
        search_lines = search_text.split("\n")
        replace_lines = replace_text.split("\n")

        for i in range(len(lines) - len(search_lines) + 1):
            if lines[i : i + len(search_lines)] == search_lines:
                lines[i : i + len(search_lines)] = replace_lines
                applied += 1
                break

    return "\n".join(lines), applied


def split_diffs_by_target(
    diff_blocks: List[Tuple[str, str]],
    *,
    code_text: str,
    changes_description_text: str,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """
    Route diff blocks to either code or changes_description based on exact line-wise match
    of SEARCH text. Returns (code_blocks, changes_desc_blocks, unmatched_blocks)

    If a SEARCH matches both targets, it's ambiguous and we raise error
    """
    code_lines = code_text.split("\n")
    desc_lines = changes_description_text.split("\n")

    code_blocks: List[Tuple[str, str]] = []
    desc_blocks: List[Tuple[str, str]] = []
    unmatched: List[Tuple[str, str]] = []

    for search_text, replace_text in diff_blocks:
        search_lines = search_text.split("\n")

        matches_code = _can_apply_linewise(code_lines, search_lines)
        matches_desc = _can_apply_linewise(desc_lines, search_lines)

        if matches_code and matches_desc:
            raise ValueError(
                "Ambiguous diff block: SEARCH matches both code and changes_description"
            )
        if matches_code:
            code_blocks.append((search_text, replace_text))
        elif matches_desc:
            desc_blocks.append((search_text, replace_text))
        else:
            unmatched.append((search_text, replace_text))

    return code_blocks, desc_blocks, unmatched
