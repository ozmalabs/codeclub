"""
treefrag.py — Structural code compression inspired by Stingy Context (arXiv:2601.19929).

Stingy Context achieves 18:1 reduction via TREEFRAG:
  1. Parse code into an AST.
  2. Decompose into reusable function/class fragments.
  3. Identify duplicates via structural hashing.
  4. Replace bodies with references; emit a fragment dictionary header.

Uses tree-sitter for language-agnostic parsing (Python, JavaScript/JSX, TypeScript).

Two modes:
  - stub(code): replace all function bodies with signature + docstring + "..."
    Returns (compressed_code, SourceMap) — the SourceMap enables round-trip expansion.
  - treefrag(files): deduplicate across files, emit fragment dict + stub references
    (Stingy Context-style)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

Language = Literal["python", "javascript", "typescript", "csharp"]


def _detect_language(filename: str) -> Language:
    """Infer language from file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("js", "jsx", "mjs", "cjs"):
        return "javascript"
    if ext in ("ts", "tsx", "mts", "cts"):
        return "typescript"
    if ext == "cs":
        return "csharp"
    return "python"


def _get_ts_parser(language: Language):
    """Return a tree-sitter (Parser, Language) pair for the requested language."""
    from tree_sitter import Language as TSLanguage, Parser
    if language == "python":
        import tree_sitter_python as tslang
    elif language in ("javascript", "typescript"):
        # tree-sitter-javascript handles JSX; use it for TS too (good enough for stub extraction)
        import tree_sitter_javascript as tslang
    elif language == "csharp":
        import tree_sitter_c_sharp as tslang
    else:
        import tree_sitter_python as tslang
    lang = TSLanguage(tslang.language())
    return Parser(lang), lang


# ---------------------------------------------------------------------------
# Source map — records how compressed lines map back to original lines
# ---------------------------------------------------------------------------

@dataclass
class StubEntry:
    """One stubbed function/arrow-function slot."""
    name: str
    # 0-indexed, inclusive line numbers in the ORIGINAL file
    orig_start: int
    orig_end: int
    # 0-indexed line in the COMPRESSED file where the stub begins
    comp_start: int
    comp_end: int


@dataclass
class SourceMap:
    """
    Records the mapping between a compressed (stubbed) file and its original.

    Usage:
        compressed, smap = stub_functions(code, language="python")
        restored = expand(code, smap, llm_output=compressed_with_edits)
    """
    language: Language
    original_code: str
    stubs: list[StubEntry] = field(default_factory=list)

    def by_name(self, name: str) -> StubEntry | None:
        for s in self.stubs:
            if s.name == name:
                return s
        return None


# ---------------------------------------------------------------------------
# Single-file stubbing (LongCodeZip coarse-grained pass)
# ---------------------------------------------------------------------------

def stub_functions(
    code: str,
    language: Language = "python",
    *,
    filename: str | None = None,
    keep_docstrings: bool = True,
    max_doc_len: int = 120,
) -> tuple[str, SourceMap]:
    """
    Replace every function/method body with '...' keeping only:
      - The full signature
      - The first string literal (docstring) if short enough

    Returns:
        (compressed_code, source_map)

    The source_map records orig→compressed line mappings for each stub so
    expander.expand() can reconstruct the original file after LLM edits.
    """
    if filename is not None:
        language = _detect_language(filename)
    source_map = SourceMap(language=language, original_code=code)

    try:
        parser, _ = _get_ts_parser(language)
    except Exception:
        return code, source_map  # tree-sitter unavailable — return as-is

    try:
        tree = parser.parse(code.encode("utf-8"))
    except Exception:
        return code, source_map

    if tree.root_node.has_error and len(code.strip()) == 0:
        return code, source_map

    lines = code.splitlines(keepends=True)

    # Collect (node_start_row, node_end_row, body_start_row, name) for each function
    stubs_to_apply: list[tuple[int, int, int, str]] = []

    if language == "python":
        stubs_to_apply = _collect_python_stubs(tree.root_node, lines)
    elif language == "csharp":
        stubs_to_apply = _collect_csharp_stubs(tree.root_node, code)
    else:
        stubs_to_apply = _collect_js_stubs(tree.root_node, code)

    if not stubs_to_apply:
        return code, source_map

    # Apply replacements in reverse order to preserve line indices
    result_lines = list(lines)
    comp_offset = 0  # tracks cumulative line-count delta as we replace

    # Process forward to compute comp_start, then reverse-apply
    replacements: list[tuple[int, int, str, str, int]] = []  # (orig_start, orig_end, stub_text, name, body_start)

    for (fn_start, fn_end, body_start, name) in stubs_to_apply:
        if language == "python":
            # Signature lines end just before body_start; the colon is on the last sig line
            sig_lines = lines[fn_start: body_start]
            sig_text = "".join(sig_lines).rstrip()
            doc_text = ""
            if keep_docstrings:
                doc_text = _extract_python_docstring(lines, body_start, fn_start, max_doc_len)
            # Determine stub indent from first body line
            if body_start < len(lines):
                raw = lines[body_start]
                stub_indent = " " * (len(raw) - len(raw.lstrip()))
            else:
                stub_indent = "    "
            stub = sig_text + doc_text + f"\n{stub_indent}..."
        else:
            # JS/TS: the opening '{' is on body_start row (often same as fn_start).
            # Include that line so the function name is preserved in the stub.
            sig_end_row = body_start  # inclusive — the '{' line
            sig_lines = lines[fn_start: sig_end_row + 1]
            sig_text = "".join(sig_lines).rstrip()
            # Indent for '...' = one level inside the opening brace
            first_body_content = sig_end_row + 1
            if first_body_content < len(lines):
                raw = lines[first_body_content]
                stub_indent = " " * (len(raw) - len(raw.lstrip())) if raw.strip() else "  "
            else:
                stub_indent = "  "
            # Closing brace/semicolon on fn_end line
            closing = lines[fn_end].rstrip("\n") if fn_end < len(lines) else "}"
            stub = sig_text + f"\n{stub_indent}...\n{closing}"
        replacements.append((fn_start, fn_end, stub, name, body_start))

    # Sort reverse by start line to apply bottom-up
    replacements.sort(key=lambda x: x[0], reverse=True)

    # Track where each stub lands in the compressed output
    # We need forward-pass ordering to compute comp_start, so compute after all replacements
    result_lines = list(lines)
    for fn_start, fn_end, stub_text, name, body_start in replacements:
        stub_lines = stub_text.splitlines(keepends=True)
        if stub_lines and not stub_lines[-1].endswith("\n"):
            stub_lines[-1] += "\n"
        result_lines[fn_start: fn_end + 1] = stub_lines

    compressed = "".join(result_lines)

    # Build source map: scan compressed to find where each stub landed
    comp_lines = compressed.splitlines(keepends=True)
    _build_source_map(source_map, replacements, lines, comp_lines)

    return compressed, source_map


def _collect_python_stubs(root_node, lines: list[str]) -> list[tuple[int, int, int, str]]:
    """Walk tree-sitter Python AST for function_definition nodes."""
    results = []
    seen_ranges: set[tuple[int, int]] = set()
    _walk_python(root_node, lines, results, seen_ranges)
    return results


def _walk_python(node, lines, results, seen_ranges):
    if node.type in ("function_definition", "decorated_definition"):
        actual = node
        if node.type == "decorated_definition":
            # Find the inner function_definition
            for child in node.children:
                if child.type == "function_definition":
                    actual = child
                    break

        if actual.type == "function_definition":
            fn_start = node.start_point[0]
            fn_end = node.end_point[0]
            key = (fn_start, fn_end)

            if key not in seen_ranges:
                seen_ranges.add(key)

                # Find the name child
                name = "<anonymous>"
                for child in actual.children:
                    if child.type == "identifier":
                        name = child.text.decode("utf-8")
                        break

                # Find the body (block node)
                body_start = fn_end  # fallback
                for child in actual.children:
                    if child.type == "block":
                        body_start = child.start_point[0]
                        break

                # Only stub if body has real content (more than just a docstring/pass)
                body_lines = lines[body_start: fn_end + 1]
                non_trivial = any(
                    l.strip() not in ("...", "pass", '"""', "'''", "")
                    and not l.strip().startswith('"""')
                    and not l.strip().startswith("'''")
                    for l in body_lines
                )
                if non_trivial:
                    results.append((fn_start, fn_end, body_start, name))

    for child in node.children:
        _walk_python(child, lines, results, seen_ranges)


def _collect_js_stubs(root_node, code: str) -> list[tuple[int, int, int, str]]:
    """Walk tree-sitter JS AST for function nodes."""
    results: list[tuple[int, int, int, str]] = []
    lines = code.splitlines(keepends=True)
    _walk_js(root_node, code, lines, results, set())
    return results


def _walk_js(node, code: str, lines: list[str], results: list, seen_ranges: set):
    """Recursively collect stubgable function nodes from a JS tree."""
    handled = False

    if node.type == "function_declaration":
        name = _js_node_name(node, "identifier")
        body = _js_body_node(node, "statement_block")
        if body and node.end_point[0] > node.start_point[0]:
            key = (node.start_point[0], node.end_point[0])
            if key not in seen_ranges:
                seen_ranges.add(key)
                results.append((node.start_point[0], node.end_point[0], body.start_point[0], name))
                handled = True

    elif node.type == "method_definition":
        name = _js_node_name(node, "property_identifier")
        body = _js_body_node(node, "statement_block")
        if body and node.end_point[0] > node.start_point[0]:
            key = (node.start_point[0], node.end_point[0])
            if key not in seen_ranges:
                seen_ranges.add(key)
                results.append((node.start_point[0], node.end_point[0], body.start_point[0], name))
                handled = True

    elif node.type in ("lexical_declaration", "variable_declaration"):
        # const foo = (...) => { ... }  or  const foo = function() { ... }
        # const Comp = (...) => (...)    (JSX components)
        for decl in node.children:
            if decl.type == "variable_declarator":
                name_node = None
                arrow_fn = None
                for child in decl.children:
                    if child.type == "identifier":
                        name_node = child
                    elif child.type in ("arrow_function", "function"):
                        arrow_fn = child
                if name_node and arrow_fn:
                    name = name_node.text.decode("utf-8")
                    body = _js_arrow_body(arrow_fn)
                    if body and arrow_fn.end_point[0] > arrow_fn.start_point[0]:
                        # Use the outer declaration range for the full span
                        key = (node.start_point[0], node.end_point[0])
                        if key not in seen_ranges:
                            seen_ranges.add(key)
                            results.append((node.start_point[0], node.end_point[0], body.start_point[0], name))

    if not handled:
        for child in node.children:
            _walk_js(child, code, lines, results, seen_ranges)


def _js_node_name(node, name_type: str) -> str:
    for child in node.children:
        if child.type == name_type:
            return child.text.decode("utf-8")
    return "<anonymous>"


def _js_body_node(node, body_type: str):
    for child in node.children:
        if child.type == body_type:
            return child
    return None


def _js_arrow_body(node):
    """Find the body of an arrow function — statement_block or parenthesized_expression."""
    for child in node.children:
        if child.type in ("statement_block", "parenthesized_expression"):
            return child
    return None


# ---------------------------------------------------------------------------
# C# AST walking
# ---------------------------------------------------------------------------

def _collect_csharp_stubs(root_node, code: str) -> list[tuple[int, int, int, str]]:
    """Walk tree-sitter C# AST for method/constructor/property nodes."""
    results: list[tuple[int, int, int, str]] = []
    lines = code.splitlines(keepends=True)
    _walk_csharp(root_node, lines, results, set())
    return results


def _walk_csharp(node, lines: list[str], results: list, seen_ranges: set):
    """Recursively collect stubbable nodes from a C# tree."""
    if node.type in ("method_declaration", "constructor_declaration",
                      "operator_declaration", "conversion_operator_declaration"):
        name = "<anonymous>"
        for child in node.children:
            if child.type == "identifier":
                name = child.text.decode("utf-8")
                break
        body = None
        for child in node.children:
            if child.type == "block":
                body = child
                break
        if body and node.end_point[0] > node.start_point[0]:
            key = (node.start_point[0], node.end_point[0])
            if key not in seen_ranges:
                seen_ranges.add(key)
                results.append((node.start_point[0], node.end_point[0],
                               body.start_point[0], name))
                return  # don't recurse into body

    # Property accessors (get/set bodies)
    if node.type == "property_declaration":
        name = "<property>"
        for child in node.children:
            if child.type == "identifier":
                name = child.text.decode("utf-8")
                break
        # Find accessor_list which contains get/set blocks
        for child in node.children:
            if child.type == "accessor_list":
                for acc in child.children:
                    if acc.type == "accessor_declaration":
                        body = None
                        for c in acc.children:
                            if c.type == "block":
                                body = c
                                break
                        if body and acc.end_point[0] > acc.start_point[0]:
                            key = (acc.start_point[0], acc.end_point[0])
                            if key not in seen_ranges:
                                seen_ranges.add(key)
                                results.append((acc.start_point[0], acc.end_point[0],
                                               body.start_point[0], name))

    for child in node.children:
        _walk_csharp(child, lines, results, seen_ranges)


def _extract_python_docstring(lines: list[str], body_start: int, fn_start: int, max_len: int) -> str:
    """Try to find a short docstring on the first body line."""
    if body_start >= len(lines):
        return ""
    first = lines[body_start].strip()
    if first.startswith('"""') or first.startswith("'''"):
        quote = first[:3]
        # Single-line docstring
        if first.count(quote) >= 2 and len(first) > 6:
            doc = first.strip(quote).strip()
            if len(doc) <= max_len:
                indent = len(lines[body_start]) - len(lines[body_start].lstrip())
                return f'\n{" " * indent}{quote}{doc}{quote}'
    return ""


def _build_source_map(
    source_map: SourceMap,
    replacements: list[tuple[int, int, str, str, int]],
    orig_lines: list[str],
    comp_lines: list[str],
):
    """
    Populate source_map.stubs using deterministic line-offset arithmetic.

    Because we apply replacements bottom-up (largest orig_start first), each stub's
    position in the compressed file is:
        comp_start = orig_start + sum_of_line_deltas_from_earlier_stubs

    where delta_i = len(stub_i_lines) - (orig_end_i - orig_start_i + 1)
    for all stubs with orig_start_i < orig_start of this stub.
    """
    # Forward-sorted by orig_start
    fwd = sorted(replacements, key=lambda x: x[0])

    running_delta = 0  # cumulative line count delta applied so far
    for fn_start, fn_end, stub_text, name, body_start in fwd:
        orig_span = fn_end - fn_start + 1
        stub_line_count = len(stub_text.splitlines())

        comp_start = fn_start + running_delta
        comp_end = comp_start + stub_line_count - 1

        source_map.stubs.append(StubEntry(
            name=name,
            orig_start=fn_start,
            orig_end=fn_end,
            comp_start=max(0, comp_start),
            comp_end=max(0, comp_end),
        ))

        # Update delta for subsequent stubs
        running_delta += stub_line_count - orig_span


# ---------------------------------------------------------------------------
# Cross-file fragment deduplication (Stingy Context TREEFRAG)
# ---------------------------------------------------------------------------

class Fragment(NamedTuple):
    hash: str
    source: str
    label: str


@dataclass
class TreeFragResult:
    """Result of treefrag compression across one or more files."""
    fragment_dict: dict[str, Fragment] = field(default_factory=dict)
    compressed_files: dict[str, str] = field(default_factory=dict)
    source_maps: dict[str, SourceMap] = field(default_factory=dict)
    original_tokens: int = 0
    compressed_tokens: int = 0

    @property
    def tokens_saved(self) -> int:
        return self.original_tokens - self.compressed_tokens

    @property
    def compression_ratio(self) -> float:
        return self.tokens_saved / self.original_tokens if self.original_tokens else 0.0


def _hash_node_source(source: str) -> str:
    return "F" + hashlib.sha1(source.encode()).hexdigest()[:8].upper()


def treefrag(files: dict[str, str], *, min_body_tokens: int = 15) -> TreeFragResult:
    """
    Stingy Context TREEFRAG across multiple files.

    For each function whose body exceeds `min_body_tokens`:
      - Compute a structural hash of the body source.
      - Store the full body in the fragment dictionary (once).
      - Replace the body in the file with a `# FRAG:<hash>` reference.

    Now uses tree-sitter for language-agnostic parsing.
    Returns TreeFragResult including per-file SourceMaps for expansion.
    """
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")

    result = TreeFragResult()
    fragment_dict: dict[str, Fragment] = {}

    for filename, code in files.items():
        result.original_tokens += len(enc.encode(code))
        language = _detect_language(filename)

        compressed, smap = stub_functions(code, language)
        result.source_maps[filename] = smap

        # Further replace stub bodies with FRAG refs for deduplication
        lines = compressed.splitlines(keepends=True)
        frag_lines = list(lines)
        replacements = []

        for stub in smap.stubs:
            body_source_lines = code.splitlines(keepends=True)[stub.orig_start: stub.orig_end + 1]
            body_source = "".join(body_source_lines)
            body_tokens = len(enc.encode(body_source))
            if body_tokens < min_body_tokens:
                continue

            frag_hash = _hash_node_source(body_source)
            label = f"{filename}::{stub.name}"
            if frag_hash not in fragment_dict:
                fragment_dict[frag_hash] = Fragment(hash=frag_hash, source=body_source, label=label)

            indent_str = "    "
            if stub.comp_end < len(frag_lines):
                raw = frag_lines[stub.comp_end]
                indent_str = " " * (len(raw) - len(raw.lstrip()))
            ref_line = f"{indent_str}# FRAG:{frag_hash}  # {stub.name} body\n"
            replacements.append((stub.comp_start, stub.comp_end, ref_line))

        for start, end, ref in sorted(replacements, key=lambda x: x[0], reverse=True):
            frag_lines[start: end + 1] = [ref]

        final_compressed = "".join(frag_lines)
        result.compressed_files[filename] = final_compressed
        result.compressed_tokens += len(enc.encode(final_compressed))

    result.fragment_dict = fragment_dict
    return result


def render_fragment_dict(fragment_dict: dict[str, Fragment]) -> str:
    """Render the fragment dictionary as a header block for the prompt."""
    if not fragment_dict:
        return ""
    lines = ["# === FRAGMENT DICTIONARY (Stingy Context TREEFRAG) ===\n"]
    for frag in fragment_dict.values():
        lines.append(f"# {frag.hash} [{frag.label}]:\n")
        for src_line in frag.source.splitlines():
            lines.append(f"#   {src_line}\n")
        lines.append("#\n")
    lines.append("# === END FRAGMENT DICTIONARY ===\n\n")
    return "".join(lines)
