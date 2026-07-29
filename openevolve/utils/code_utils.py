"""
Utilities for code parsing, diffing, and manipulation
"""

import re
from typing import Dict, List, Optional, Tuple, Union


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
    r"\s*Formula:\s*(?P<formula>.*?)\s*(?=\n\s*\(\d+\)\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def extract_transformation_witnesses(explanation_text: str) -> List[Dict[str, str]]:
    """
    Pull out per-change "Witness"/"Formula" entries from an LLM explanation that
    follows the (1)/(2)/... numbered format (see
    examples/bpf_compile/prompt_templates). Only "Witness:" and "Formula:" are
    required verbatim -- whatever's between the numbered summary and "Witness:"
    (nominally an "Example:" line) is captured as-is in "detail", since models
    don't always use that exact label (e.g. "Old:"/"New:" pairs instead), and
    requiring it verbatim silently dropped the whole item, formula included.
    Entries missing a Witness or Formula line are simply not matched -- this is
    best-effort, not enforced.

    Args:
        explanation_text: Explanation text, e.g. from extract_change_explanation()

    Returns:
        List of {"summary", "detail", "witness", "formula"} dicts, in order
    """
    witnesses = []
    for match in _WITNESS_BLOCK_PATTERN.finditer(explanation_text):
        # The prompt's own "..." continuation marker sometimes gets echoed back
        # by the model as trailing filler after the last real item; drop it.
        formula = re.sub(r"\n?\.\.\.\s*$", "", match.group("formula").strip())
        witnesses.append(
            {
                "summary": match.group("summary").strip(),
                "detail": match.group("detail").strip(),
                "witness": match.group("witness").strip(),
                "formula": formula.strip(),
            }
        )
    return witnesses


def validate_smt_formula(formula: str) -> Dict[str, Optional[str]]:
    """
    Shallow-validate an SMT-LIB 2 formula snippet with z3: does it parse, and
    (if so) is it satisfiable? This does NOT check the formula against the
    program's actual semantics -- it only catches malformed/self-contradictory
    formulas so a reviewer isn't handed garbage. z3-solver is an optional
    dependency of this check, not of OpenEvolve itself.

    Args:
        formula: SMT-LIB 2 snippet (declare-const/declare-fun + assert lines)

    Returns:
        {"parses": "true"/"false", "check": "sat"/"unsat"/"unknown"/None,
         "error": error message or None}
    """
    try:
        import z3
    except ImportError:
        return {"parses": None, "check": None, "error": "z3-solver not installed"}

    try:
        assertions = z3.parse_smt2_string(formula)
    except z3.Z3Exception as e:
        return {"parses": "false", "check": None, "error": str(e)}

    try:
        solver = z3.Solver()
        solver.add(assertions)
        result = str(solver.check())
    except Exception as e:  # pragma: no cover - defensive, z3 check() rarely throws
        return {"parses": "true", "check": None, "error": str(e)}

    return {"parses": "true", "check": result, "error": None}


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
