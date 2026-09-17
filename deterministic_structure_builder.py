#!/usr/bin/env python3
"""
deterministic_structure_builder.py

Deterministic, general-purpose Stage 1 replacement: derives CELL_STRUCTURE
directly from RAW_DATA's row/col/merge_range facts instead of asking an LLM
to guess it. No sheet-specific coordinates, labels, or terminology are
hardcoded here - every decision below is made from the shape of whatever
RAW_DATA is actually supplied.

Layout-driven design: rather than assuming a single global group-header
row, a single global subheader row, or a single global label column for
the whole sheet, the sheet is first split into independent REGIONS (each
one governed by its own group-header candidate, or no group at all before
the first one). Every subsequent decision - subheader detection, which
column holds a row's label, which cells belong to which group - is made
fresh within each region, and a row's label is found independently for
that row (its leftmost non-structural, non-group-owned populated cell),
never from a sheet-wide or region-wide column vote. This lets the same
sheet contain multiple independent tables, or a table plus unrelated
standalone rows, without one area's shape distorting another's.

Architecture is unchanged:

    Excel -> RAW_DATA JSON -> CELL_STRUCTURE JSON -> FINAL STRUCTURED JSON

This script only replaces how the middle artifact (CELL_STRUCTURE) gets
produced for the common/well-structured case. Its output format matches
what structure_relationship_prompt.txt expects: plain cell references,
"<context>:<value>" colon paths, and arrays of either.

Usage:
    python deterministic_structure_builder.py --raw-data path/to/raw.json --out path/to/structure.json
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict


def col_letters_to_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def parse_ref(ref: str):
    m = re.match(r"([A-Z]+)(\d+)", ref)
    col = col_letters_to_num(m.group(1))
    row = int(m.group(2))
    return row, col


def parse_merge_range(merge_range: str):
    start, end = merge_range.split(":")
    r1, c1 = parse_ref(start)
    r2, c2 = parse_ref(end)
    return min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)


def build_structure(raw_data: dict):
    """Returns (cell_structure_dict, diagnostics_dict)."""
    cells = raw_data["cells"]
    warnings = []

    if not cells:
        return {}, {"total_raw_cells": 0, "represented_cells": 0,
                     "unaccounted_cells": [], "warnings": []}

    by_cell = {c["cell"]: c for c in cells}
    by_row = defaultdict(list)
    for c in cells:
        by_row[c["row"]].append(c)
    max_row_in_sheet = max(c["row"] for c in cells)

    def merge_span(cell_ref):
        rc = by_cell[cell_ref]
        mr = rc.get("merge_range")
        if not mr:
            return None
        return parse_merge_range(mr)  # (min_row, max_row, min_col, max_col)

    # ---- Step 1: classify merges (sheet-wide - these are raw geometric facts) ----
    wide_row_merges = []   # (row, min_col, max_col, cell_ref) - spans >1 col, 1 row
    tall_col_merges = []   # (col, min_row, max_row, cell_ref) - spans >1 row, 1 col

    for c in cells:
        mr = c.get("merge_range")
        if not mr:
            continue
        min_row, max_row, min_col, max_col = parse_merge_range(mr)
        if min_row == max_row and max_col > min_col:
            wide_row_merges.append((min_row, min_col, max_col, c["cell"]))
        elif min_col == max_col and max_row > min_row:
            tall_col_merges.append((min_col, min_row, max_row, c["cell"]))
        elif max_row > min_row and max_col > min_col:
            warnings.append(
                f"{c['cell']}: merge_range {mr} spans both multiple rows and "
                f"multiple columns - ambiguous, not classified as a group or section."
            )

    # ---- Step 2: sections (row-group merges) - sheet-wide, self-scoping by
    # their own row range, so no "pick one" ambiguity exists here. ----
    section_by_row = {}
    section_cells = []
    for col, min_row, max_row, ref in tall_col_merges:
        section_cells.append(ref)
        for r in range(min_row, max_row + 1):
            section_by_row[r] = ref
    section_anchor_cells = set(section_cells)

    # ---- Step 3: partition the sheet into independent REGIONS. A row with
    # >=2 sibling wide-row merges is a group-header CANDIDATE - but an
    # ordinary data row can coincidentally have the same shape (e.g. one
    # value merged across each group's columns, just for that single row -
    # a locally-collapsed reading, not a new table). What distinguishes a
    # genuine new table from a coincidental repeat is whether its merges
    # cover a DIFFERENT set of columns than the table already in progress:
    # a real second table defines its own column ownership, whereas a
    # coincidental repeat reuses the very same columns the current region
    # already owns. So a candidate only opens a new region when its column
    # signature differs from the currently-open region's; otherwise it's
    # left as an ordinary row inside the region already in progress. ----
    merges_by_row = defaultdict(list)
    for row, min_col, max_col, ref in wide_row_merges:
        merges_by_row[row].append((min_col, max_col, ref))

    candidate_rows = sorted(
        row for row, entries in merges_by_row.items() if len(entries) >= 2
    )

    def signature(row):
        return frozenset((mc, xc) for mc, xc, _ in merges_by_row[row])

    populated_rows = set(by_row.keys())

    group_header_rows = []
    last_signature = None
    for row in candidate_rows:
        sig = signature(row)
        if sig == last_signature:
            # Same column shape as the table already in progress - a
            # coincidental per-row collapsed merge, not a new table.
            continue
        if group_header_rows and (row - 1) in populated_rows:
            # A genuinely independent second table is conventionally set
            # apart by at least one blank row; this candidate is directly
            # adjacent to existing content, so - despite the differing
            # column shape - treat it as an ordinary row of the table
            # already in progress rather than a new one (e.g. a row whose
            # own label happens to be merged too, alongside merged group
            # values, which coincidentally looks like a header).
            continue
        group_header_rows.append(row)
        last_signature = sig

    regions = []
    if group_header_rows:
        # region 0: rows before the first group header, no groups
        first_header = group_header_rows[0]
        if first_header > 1:
            regions.append({"start": 1, "end": first_header - 1,
                             "header_row": None, "groups": []})
        for i, header_row in enumerate(group_header_rows):
            end = (group_header_rows[i + 1] - 1) if i + 1 < len(group_header_rows) \
                else max_row_in_sheet
            groups = [{"cell": ref, "min_col": mc, "max_col": xc}
                      for mc, xc, ref in sorted(merges_by_row[header_row])]
            regions.append({"start": header_row, "end": end,
                             "header_row": header_row, "groups": groups})
    else:
        regions.append({"start": 1, "end": max_row_in_sheet, "header_row": None, "groups": []})
        warnings.append(
            "No group-header row found anywhere (need >=2 sibling multi-column, "
            "single-row merges on the same row) - treating the whole sheet as one "
            "no-group region."
        )

    structure = {}
    used_cells = set()

    def _place(path, label_ref, value):
        node = structure
        for key in path:
            node = node.setdefault(key, {})
        node[label_ref] = value

    # ---- Process each region independently ----
    for region in regions:
        start, end = region["start"], region["end"]
        header_row = region["header_row"]
        groups = region["groups"]
        group_owned_cols = {c for g in groups for c in range(g["min_col"], g["max_col"] + 1)}

        def group_for_col(col, groups=groups):
            for g in groups:
                if g["min_col"] <= col <= g["max_col"]:
                    return g
            return None

        # ---- Step 4 (per region): subheader row detection ----
        subheader_row = None
        subheader_by_col = {}
        if groups:
            for r in range(header_row + 1, end + 1):
                row_cells = by_row.get(r, [])
                counts = defaultdict(int)
                per_group_cells = defaultdict(list)
                for rc in row_cells:
                    if rc.get("merge_range"):
                        continue  # merged cells don't count as individual subheader slots
                    g = group_for_col(rc["col"])
                    if g is not None:
                        counts[g["cell"]] += 1
                        per_group_cells[g["cell"]].append(rc)
                groups_with_multi = [g for g, n in counts.items() if n >= 2]
                if len(groups_with_multi) >= 2:
                    subheader_row = r
                    for g_cell, rcs in per_group_cells.items():
                        for rc in rcs:
                            subheader_by_col[rc["col"]] = rc["cell"]
                    break

        # ---- Step 5 (per region): per (section, group) evidence that a
        # subheader split is genuinely used there ----
        context_applies = defaultdict(bool)  # key: (section_ref_or_None, group_cell)
        if groups and subheader_row is not None:
            for r in range(subheader_row + 1, end + 1):
                row_cells = by_row.get(r, [])
                sect = section_by_row.get(r)
                for g in groups:
                    unmerged_in_group = [
                        rc for rc in row_cells
                        if g["min_col"] <= rc["col"] <= g["max_col"] and not rc.get("merge_range")
                    ]
                    if len(unmerged_in_group) >= 2:
                        context_applies[(sect, g["cell"])] = True

        def resolve_value_ref(rc, group, section_ref,
                               subheader_row=subheader_row,
                               subheader_by_col=subheader_by_col,
                               context_applies=context_applies):
            """Decide plain-ref vs '<context>:<value>' for one populated data cell."""
            cell_ref = rc["cell"]
            row = rc["row"]

            if subheader_row is None or row <= subheader_row:
                return cell_ref  # above/at the subheader tier - no context concept yet

            if not context_applies.get((section_ref, group["cell"]), False):
                return cell_ref  # this section+group never shows a genuine split

            span = merge_span(cell_ref)
            if span is not None:
                _, _, min_col, max_col = span
                distinct_subheader_cols = {
                    col for col in range(min_col, max_col + 1) if col in subheader_by_col
                }
                if len(distinct_subheader_cols) >= 2:
                    return cell_ref  # this row collapses multiple subheader slots - direct value

            ctx = subheader_by_col.get(rc["col"])
            return f"{ctx}:{cell_ref}" if ctx else cell_ref

        structural_rows = {header_row, subheader_row} - {None}

        # ---- Steps 6-9 (per row within this region): find the label
        # independently for each row, then its value(s). ----
        for r in range(start, end + 1):
            if r in structural_rows:
                continue
            row_cells = by_row.get(r, [])
            if not row_cells:
                continue

            non_structural = [
                rc for rc in row_cells
                if rc["col"] not in group_owned_cols and rc["cell"] not in section_anchor_cells
            ]
            if not non_structural:
                continue  # this row's only populated cell(s) were structural (e.g. a section anchor)

            non_structural.sort(key=lambda rc: rc["col"])
            label_rc = non_structural[0]
            label_ref = label_rc["cell"]
            other_non_group = non_structural[1:]  # non-group cells besides the label itself
            section_ref = section_by_row.get(r)

            matched_any_group = False
            for g in groups:
                data_cells = [
                    rc for rc in row_cells
                    if g["min_col"] <= rc["col"] <= g["max_col"]
                ]
                if not data_cells:
                    continue
                matched_any_group = True
                data_cells.sort(key=lambda rc: rc["col"])
                refs = [resolve_value_ref(dc, g, section_ref) for dc in data_cells]
                value = refs[0] if len(refs) == 1 else refs

                path = [g["cell"]]
                if section_ref is not None:
                    path.append(section_ref)
                _place(path, label_ref, value)

                used_cells.add(label_ref)
                for dc in data_cells:
                    used_cells.add(dc["cell"])

            if not matched_any_group:
                # No group owns any data on this row (either a no-group
                # region, or a row with no group-column data at all) -
                # every other non-structural cell on the row is this
                # label's value(s) directly.
                if len(other_non_group) == 0:
                    warnings.append(
                        f"{label_ref} (row {r}, col {label_rc['col']}): no other "
                        f"populated, non-structural cell found on this row to pair "
                        f"it with - omitted from CELL_STRUCTURE."
                    )
                    continue
                refs = [rc["cell"] for rc in other_non_group]
                value = refs[0] if len(refs) == 1 else refs
                structure[label_ref] = value
                used_cells.add(label_ref)
                for rc in other_non_group:
                    used_cells.add(rc["cell"])

    # ---- Coverage audit ----
    def collect_refs(node, acc):
        if isinstance(node, dict):
            for k, v in node.items():
                acc.add(k)
                collect_refs(v, acc)
        elif isinstance(node, list):
            for item in node:
                collect_refs(item, acc)
        elif isinstance(node, str):
            if ":" in node:
                a, b = node.split(":", 1)
                acc.add(a)
                acc.add(b)
            else:
                acc.add(node)

    all_raw_refs = {c["cell"] for c in cells}
    referenced = set()
    collect_refs(structure, referenced)
    represented = all_raw_refs & referenced
    unaccounted = sorted(all_raw_refs - referenced, key=lambda ref: parse_ref(ref))

    for ref in unaccounted:
        rc = by_cell[ref]
        warnings.append(
            f"{ref} (row {rc['row']}, col {rc['col']}): populated cell was not "
            f"confidently classified - omitted from CELL_STRUCTURE."
        )

    diagnostics = {
        "total_raw_cells": len(all_raw_refs),
        "represented_cells": len(represented),
        "unaccounted_cells": unaccounted,
        "warnings": warnings,
    }

    return structure, diagnostics


def main():
    parser = argparse.ArgumentParser(
        description="Deterministically derive CELL_STRUCTURE from a RAW_DATA JSON file."
    )
    parser.add_argument("--raw-data", required=True, help="Path to the RAW_DATA JSON file")
    parser.add_argument("--out", required=True, help="Output path for the CELL_STRUCTURE JSON")
    args = parser.parse_args()

    with open(args.raw_data, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    structure, diagnostics = build_structure(raw_data)

    now = datetime.datetime.now()
    timestamp_for_name = now.strftime("%d%m%Y_%H%M%S")
    base, ext = os.path.splitext(args.out)
    stamped_out = f"{base}_{timestamp_for_name}{ext}"

    out_dir = os.path.dirname(stamped_out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(stamped_out, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)
    print(f"Wrote {stamped_out}")

    meta = {
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "generator": "deterministic_structure_builder.py",
        "raw_data": args.raw_data,
    }
    meta_path = os.path.splitext(stamped_out)[0] + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    diagnostics_path = os.path.splitext(stamped_out)[0] + ".warnings.json"
    with open(diagnostics_path, "w", encoding="utf-8") as f:
        json.dump(diagnostics, f, indent=2, ensure_ascii=False)

    print(f"Coverage: {diagnostics['represented_cells']}/{diagnostics['total_raw_cells']} "
          f"populated cells represented.")
    if diagnostics["unaccounted_cells"]:
        print(f"UNACCOUNTED ({len(diagnostics['unaccounted_cells'])}): "
              f"{diagnostics['unaccounted_cells']}", file=sys.stderr)
    if diagnostics["warnings"]:
        print(f"Wrote {diagnostics_path} ({len(diagnostics['warnings'])} warning(s))")
        for w in diagnostics["warnings"]:
            print(f"  WARNING: {w}", file=sys.stderr)
    else:
        print("No warnings - every populated cell was classified.")


if __name__ == "__main__":
    main()
