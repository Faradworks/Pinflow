"""Shared helpers for chip builders."""

import re
import tempfile
from pathlib import Path
from typing import Optional

import kicad_sch_api as ksa


def sch_to_string(
    sch: ksa.Schematic,
    *,
    preserve_lib_symbols_from: Optional[str] = None,
) -> str:
    """Serialize a Schematic to its (kicad_sch ...) S-expression text.

    kicad-sch-api 0.5.x has no direct `to_string()`; round-trip via tempfile,
    then post-process to strip emitter bugs that break KiCad parsing.

    `preserve_lib_symbols_from`: optional source-file text whose
    `(lib_symbols ...)` block donates symbol definitions for any lib_id still
    referenced by a component but missing from ksa's freshly-rebuilt block.
    See `_restore_inline_lib_symbols` for the bug it works around.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False
    ) as f:
        tmp_path = Path(f.name)
    try:
        sch.save(str(tmp_path))
        text = _strip_corrupt_private_props(tmp_path.read_text())
        if preserve_lib_symbols_from is not None:
            text = _restore_inline_lib_symbols(text, preserve_lib_symbols_from)
        return text
    finally:
        tmp_path.unlink(missing_ok=True)


# Top-level (kicad_sch ...) children that are file-only metadata; eeschema's
# paste handler rejects clipboard payloads containing these wrappers.
_FILE_ONLY_HEADS = frozenset(
    {
        "version",
        "generator",
        "generator_version",
        "uuid",
        "paper",
        "title_block",
        "sheet_instances",
        "embedded_fonts",
    }
)


def to_clipboard_format(sch_text: str) -> str:
    """Convert a `(kicad_sch ...)` file payload into eeschema's clipboard format.

    eeschema's Ctrl+C / Ctrl+V format is the FLAT inner forms of a kicad_sch —
    `(lib_symbols ...) (symbol ...) (wire ...) (label ...) ...` — without the
    outer `(kicad_sch ...)` wrapper or per-document metadata. Pasting a full
    kicad_sch document drops it as a literal text block instead.
    """
    start = sch_text.find("(kicad_sch")
    if start < 0:
        raise ValueError("not a kicad_sch payload")
    end = _matching_close(sch_text, start)
    if end < 0:
        raise ValueError("unbalanced kicad_sch payload")

    # Step into kicad_sch's body: skip `(kicad_sch` plus following whitespace.
    body_start = sch_text.find("\n", start)
    body_start = body_start + 1 if body_start != -1 else start + len("(kicad_sch")

    out: list[str] = []
    pos = body_start
    while pos < end:
        # skip whitespace between siblings
        while pos < end and sch_text[pos] in " \t\n\r":
            pos += 1
        if pos >= end or sch_text[pos] != "(":
            break
        form_close = _matching_close(sch_text, pos)
        if form_close < 0 or form_close > end:
            break
        head_match = re.match(r"\(\s*([A-Za-z_][\w]*)", sch_text[pos:])
        head = head_match.group(1) if head_match else ""
        if head not in _FILE_ONLY_HEADS:
            out.append(sch_text[pos : form_close + 1])
        pos = form_close + 1

    return "\n".join(out) + "\n"


def _matching_close(text: str, open_idx: int) -> int:
    """Return index of the `)` matching `text[open_idx] == '('`, treating
    `"..."` as opaque. Returns -1 if unbalanced.
    """
    if open_idx >= len(text) or text[open_idx] != "(":
        return -1
    depth = 0
    in_str = False
    escape = False
    for i in range(open_idx, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _find_lib_symbols_block(text: str) -> Optional[tuple[int, int]]:
    """Return (open_idx, close_idx) of the `(lib_symbols ...)` form, or None."""
    m = re.search(r"\(\s*lib_symbols\b", text)
    if not m:
        return None
    open_idx = m.start()
    close_idx = _matching_close(text, open_idx)
    if close_idx < 0:
        return None
    return open_idx, close_idx


def _parse_inline_symbols(block_text: str) -> dict[str, str]:
    """Map lib_id → full `(symbol "lib_id" ...)` S-exp, from a (lib_symbols ...) block."""
    out: dict[str, str] = {}
    body_match = re.match(r"\(\s*lib_symbols\b", block_text)
    if not body_match:
        return out
    pos = body_match.end()
    end = len(block_text) - 1  # the matching `)`
    while pos < end:
        while pos < end and block_text[pos] in " \t\n\r":
            pos += 1
        if pos >= end or block_text[pos] != "(":
            break
        close = _matching_close(block_text, pos)
        if close < 0 or close > end:
            break
        form = block_text[pos:close + 1]
        head_match = re.match(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"', form)
        if head_match:
            out[head_match.group(1)] = form
        pos = close + 1
    return out


def _restore_inline_lib_symbols(saved_text: str, source_text: str) -> str:
    """Splice inline-only symbol defs from `source_text` into `saved_text`.

    kicad-sch-api 0.5.x rebuilds (lib_symbols ...) from its on-disk symbol
    cache on save (see schematic.py:1668-1681), dropping any symbol whose
    lib_id isn't resolvable on disk. Copy-pasted / inline-only symbols live
    only in the file's (lib_symbols ...) block, so they get nuked on the
    first round-trip — KiCad then renders them as "??".

    This restores them: any lib_id that lives in `source_text`'s lib_symbols
    block, is still referenced by some component in `saved_text`, but isn't
    in `saved_text`'s lib_symbols block, is spliced back in.
    """
    src_block = _find_lib_symbols_block(source_text)
    if src_block is None:
        return saved_text
    src_block_text = source_text[src_block[0]:src_block[1] + 1]
    src_symbols = _parse_inline_symbols(src_block_text)
    if not src_symbols:
        return saved_text

    dst_block = _find_lib_symbols_block(saved_text)
    if dst_block is None:
        return saved_text
    dst_block_text = saved_text[dst_block[0]:dst_block[1] + 1]
    dst_symbols = _parse_inline_symbols(dst_block_text)

    referenced = set(re.findall(r'\(\s*lib_id\s+"((?:[^"\\]|\\.)*)"\s*\)', saved_text))
    missing = [
        lib_id for lib_id in src_symbols
        if lib_id not in dst_symbols and lib_id in referenced
    ]
    if not missing:
        return saved_text

    insert = "\n\t\t" + "\n\t\t".join(src_symbols[lib_id] for lib_id in missing)
    # Splice before the closing `)` of the dst (lib_symbols ...) block.
    if not dst_block_text.endswith(")"):
        return saved_text
    new_block = dst_block_text[:-1].rstrip() + insert + "\n\t)"
    return saved_text[:dst_block[0]] + new_block + saved_text[dst_block[1] + 1:]


def _strip_corrupt_private_props(text: str) -> str:
    """Remove `(property "private" ...)` blocks that kicad-sch-api 0.5.x
    serializes with broken (unquoted) value strings.

    Affects symbols that carry KLC documentation properties (e.g. RP2040).
    The properties are docs-only, removing them is functionally safe.
    """
    out = []
    i = 0
    needle = '(property "private"'
    while i < len(text):
        idx = text.find(needle, i)
        if idx == -1:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        # Walk forward from `(` finding the matching `)`, treating "..." as opaque.
        depth = 0
        j = idx
        in_str = False
        while j < len(text):
            c = text[j]
            if c == '"' and (j == 0 or text[j - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        # Also drop the leading whitespace/newline before the stripped block
        while out and out[-1] and out[-1][-1] in " \t":
            out[-1] = out[-1].rstrip(" \t")
        if out and out[-1].endswith("\n"):
            out[-1] = out[-1].rstrip("\n") + "\n"
        i = j
    return "".join(out)
