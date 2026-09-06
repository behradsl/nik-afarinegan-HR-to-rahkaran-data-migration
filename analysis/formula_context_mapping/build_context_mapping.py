"""
Map destination HCM3.FormulaContext (EmployeeStatuteCalc) vocabulary
to tokens/patterns found in source statute/payroll formulas.

Standalone analysis (not a migration step). Produces:
  - context_inventory.csv
  - source_token_inventory.csv
  - token_mapping.csv
  - leftovers.md
  - mapping_summary.md

Usage (repo root):
  python analysis/formula_context_mapping/build_context_mapping.py
"""
from __future__ import annotations

import csv
import json
import re
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import pyodbc

warnings.filterwarnings("ignore", category=UserWarning)

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"

# Known source factor-id refs seen in formulas → classification hints
KNOWN_NON_STATUTE_HINTS = {
    10: "attendance_days",          # روزهای کارکرد
    12014: "attendance_deduction",  # روزهای کسر سختی کار
    140: "payroll_other",
    141: "payroll_other",
    150: "payroll_other",           # هیئت تخلفات
    217: "payroll_other",           # استعلاجی
}


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def load_context(dest_cnxn):
    xml = pd.read_sql(
        """
        SELECT CAST(XmlObject AS nvarchar(max)) AS XmlObject
        FROM HCM3.FormulaContext
        WHERE [Key] = N'EmployeeStatuteCalc'
        """,
        dest_cnxn,
    ).iloc[0]["XmlObject"]
    root = ET.fromstring(xml)

    fields = []
    functions = []
    dynamics = []
    simples = []
    expressions = []

    for el in root.iter("Field"):
        fields.append(
            {
                "Kind": "Field",
                "Caption": el.attrib.get("Caption"),
                "Value": el.attrib.get("Value"),
                "Type": el.attrib.get("Type"),
                "Description": el.attrib.get("Description"),
            }
        )
    for el in root.iter("Function"):
        functions.append(
            {
                "Kind": "Function",
                "Caption": el.attrib.get("Caption"),
                "Value": el.attrib.get("Value"),
                "Type": el.attrib.get("Type"),
                "Description": el.attrib.get("Description"),
            }
        )
    for el in root.iter("DynamicElements"):
        dynamics.append(
            {
                "Kind": "DynamicElements",
                "Caption": el.attrib.get("Caption"),
                "Value": el.attrib.get("Value"),
                "Type": el.attrib.get("Type"),
                "Description": el.attrib.get("Description"),
            }
        )
    for el in root.iter("Simple"):
        simples.append(
            {
                "Kind": "Simple",
                "Caption": el.attrib.get("Caption"),
                "Value": el.attrib.get("Value"),
                "Type": el.attrib.get("Type"),
            }
        )
    for el in root.iter("ExpressionItem"):
        if el.attrib.get("Value"):
            expressions.append(
                {
                    "Kind": "ExpressionItem",
                    "Caption": el.attrib.get("Caption"),
                    "Value": el.attrib.get("Value"),
                }
            )

    inventory = fields + functions + dynamics + simples + expressions
    by_value = {}
    by_caption = defaultdict(list)
    for row in inventory:
        v = row.get("Value")
        if v:
            by_value[str(v)] = row
        c = row.get("Caption")
        if c:
            by_caption[str(c).strip()].append(row)
    return inventory, by_value, by_caption, root


def inventory_source_tokens(source_cnxn, dest_cnxn):
    mapped = pd.read_sql(
        "SELECT SourcePayrollFactorID FROM master.dbo.StatuteFactorMigrationMapping",
        dest_cnxn,
    )
    ids = [int(x) for x in mapped["SourcePayrollFactorID"].tolist()]
    id_list = ",".join(str(x) for x in ids)

    formulas = pd.read_sql(
        f"""
        SELECT CAST(PAY_PbfFormula AS nvarchar(max)) AS FormulaSql, 'BackFormula' AS Src
        FROM dbo.PAY_PayrollBackFormula
        WHERE PAY_PfID_fk IN ({id_list})
          AND ISNULL(PAY_PbfActive, 1) = 1
          AND NULLIF(LTRIM(RTRIM(PAY_PbfFormula)), '') IS NOT NULL
        UNION ALL
        SELECT CAST(PAY_PfFormula AS nvarchar(max)), 'PfFormula'
        FROM dbo.PAY_PayrollFactor
        WHERE PAY_PfID IN ({id_list})
          AND NULLIF(LTRIM(RTRIM(PAY_PfFormula)), '') IS NOT NULL
        """,
        source_cnxn,
    )

    # Prefer OutputExpr from property migration (post ET-split) when available
    try:
        outputs = pd.read_sql(
            """
            SELECT OutputExpr
            FROM master.dbo.StatuteFactorPropertyMigrationMapping
            WHERE OutputExpr IS NOT NULL
              AND LTRIM(RTRIM(OutputExpr)) <> N''
            """,
            dest_cnxn,
        )
        output_texts = outputs["OutputExpr"].astype(str).tolist()
    except Exception:
        output_texts = []

    texts = formulas["FormulaSql"].astype(str).tolist()

    token_patterns = {
        "Self": re.compile(r"\bSelf\b", re.I),
        "BFactor_A": re.compile(r"\bBFactor(\d+)_A\b", re.I),
        "Factor_A": re.compile(r"(?<![A-Za-z])Factor(\d+)_A\b", re.I),
        "BFactor_TM": re.compile(r"\bBFactor(\d+)_TM\b", re.I),
        "FxPay_PersonnelKind": re.compile(r"\bFxPay_PersonnelKind\b", re.I),
        "FxPay_PersonnelKind2": re.compile(r"\bFxPay_PersonnelKind2\b", re.I),
        "FxPay_MonthDayWithPF": re.compile(r"\bFxPay_MonthDayWithPF\b", re.I),
        "case_when": re.compile(r"\bcase\b", re.I),
        "round": re.compile(r"\bround\s*\(", re.I),
        "convert_numeric": re.compile(r"\bconvert\s*\(\s*numeric\b", re.I),
        "personnel_id_arg": re.compile(r"\btbl_personnelid_fk\b", re.I),
        "pay_monthid_fk": re.compile(r"\bpay_monthid_fk\b", re.I),
        "PAY_PfID_fk": re.compile(r"\bPAY_PfID_fk\b", re.I),
        "mul": re.compile(r"\*"),
        "div": re.compile(r"/"),
        "add": re.compile(r"(?<![<>!=])\+(?!=)"),
        "sub": re.compile(r"(?<![<>!=])-(?!=)"),
        "parens": re.compile(r"[()]"),
    }

    pattern_counts = Counter()
    factor_ids = Counter()
    for t in texts + output_texts:
        for name, rx in token_patterns.items():
            hits = rx.findall(t)
            if not hits:
                continue
            if name in ("BFactor_A", "Factor_A", "BFactor_TM"):
                pattern_counts[name] += len(hits)
                for hid in hits:
                    factor_ids[int(hid)] += 1
            else:
                pattern_counts[name] += len(hits)

    # Distinct OutputExpr shapes (post-split)
    expr_counter = Counter()
    for e in output_texts:
        expr_counter[re.sub(r"\s+", " ", e.strip())] += 1

    # Factor metadata
    if factor_ids:
        flist = ",".join(str(i) for i in sorted(factor_ids))
        meta = pd.read_sql(
            f"""
            SELECT PAY_PfID, PAY_PfName, PAY_PfFieldName
            FROM dbo.PAY_PayrollFactor
            WHERE PAY_PfID IN ({flist})
            """,
            source_cnxn,
        )
        meta_map = {
            int(r["PAY_PfID"]): (
                str(r["PAY_PfName"] or "").strip(),
                str(r["PAY_PfFieldName"] or "").strip(),
            )
            for _, r in meta.iterrows()
        }
    else:
        meta_map = {}

    statute_mapped = set(
        int(x)
        for x in pd.read_sql(
            "SELECT SourcePayrollFactorID FROM master.dbo.StatuteFactorMigrationMapping",
            dest_cnxn,
        )["SourcePayrollFactorID"]
    )

    factor_rows = []
    for fid, cnt in factor_ids.most_common():
        name, field = meta_map.get(fid, ("?", "?"))
        factor_rows.append(
            {
                "SourceFactorID": fid,
                "Occurrences": cnt,
                "Name": name,
                "FieldName": field,
                "InStatuteFactorMap": fid in statute_mapped,
                "Hint": KNOWN_NON_STATUTE_HINTS.get(fid, ""),
            }
        )

    token_rows = [
        {"TokenPattern": k, "Occurrences": v}
        for k, v in pattern_counts.most_common()
    ]
    return token_rows, factor_rows, expr_counter, texts, output_texts


def build_mappings(by_value, by_caption, factor_rows, expr_counter):
    """
    Produce mapping rows: SourceConcept -> DestContext (+ status).
    """
    mappings = []

    def add(
        source,
        dest_value,
        dest_caption,
        status,
        notes,
        category,
        evidence="",
    ):
        mappings.append(
            {
                "Category": category,
                "SourceConcept": source,
                "DestValue": dest_value or "",
                "DestCaption": dest_caption or "",
                "Status": status,  # mapped | partial | leftover | eliminated
                "Notes": notes,
                "Evidence": evidence,
            }
        )

    # --- Operators / control flow ---
    add(
        "case / when / then / else / end",
        "if / else if / else",
        "اگر / در غیراینصورت",
        "mapped",
        "Source T-SQL CASE → Rahkaran C# if/else ExpressionItems in FormulaContext",
        "control_flow",
    )
    for op, note in [
        ("*", "multiply"),
        ("/", "divide"),
        ("+", "add"),
        ("-", "subtract"),
        ("()", "grouping"),
    ]:
        add(op, op, op, "mapped", f"Same arithmetic operator ({note})", "operator")

    # --- Math ---
    round_fn = by_value.get("Round") or (by_caption.get("روند") or [None])[0]
    add(
        "round(...)",
        (round_fn or {}).get("Value", "Round"),
        (round_fn or {}).get("Caption", "روند"),
        "mapped",
        "System.Math Round in FormulaContext",
        "function",
    )
    to_dec = by_value.get("ToDecimal") or (by_caption.get("تبدیل به عدد") or [None])[0]
    add(
        "convert(numeric, ...)",
        (to_dec or {}).get("Value", "ToDecimal"),
        (to_dec or {}).get("Caption", "تبدیل به عدد"),
        "mapped",
        "SQL convert(numeric) → ToDecimal (or ToInteger when whole)",
        "function",
    )

    # --- Self ---
    add(
        "Self",
        "",
        "",
        "leftover",
        "No direct FormulaContext token. In source = base amount of the factor "
        "being calculated before day-proration. Dest formula usually *computes* "
        "that value; needs a design choice (base field, prior factor, or rewrite "
        "without Self).",
        "self",
        evidence=f"appears in many OutputExpr; top shapes include Self*BFactor10_A/30",
    )

    # --- PersonnelKind already handled by property split ---
    add(
        "dbo.FxPay_PersonnelKind / Kind2 / EmploymentType",
        "",
        "",
        "eliminated",
        "ET branching removed in step 17 by StatuteFactorProperty per employment type; "
        "should not appear inside dest FormulaBody.",
        "dispatcher",
    )
    add(
        "tbl_personnelID_fk (Kind arg only)",
        "$Employee.ID",
        "شناسه",
        "eliminated",
        "Only used as Kind dispatcher argument; gone after ET split.",
        "parameter",
    )

    # --- MonthDay helper ---
    day_diff = by_value.get("GetDayDiff")
    add(
        "dbo.FxPay_MonthDayWithPF(...)",
        (day_diff or {}).get("Value", ""),
        (day_diff or {}).get("Caption", ""),
        "leftover",
        "Source UDF returns payable days for person/month/factor. Closest dest helpers "
        "are calendar/day-diff or attendance factor APIs, but not 1:1. Likely needs a "
        "dedicated attendance/statute helper factor.",
        "udf",
    )
    add(
        "pay_monthid_fk",
        "GetYearMonth / ApplyDate",
        "سال/ماه / تاریخ اجرای حکم",
        "partial",
        "Can derive from $EmployeeStatute.ApplyDate via YearMonthHelper; not a raw MonthID param.",
        "parameter",
    )
    add(
        "PAY_PfID_fk",
        "",
        "",
        "leftover",
        "Current factor id in source UDF call; dest formula is already scoped to one factor property.",
        "parameter",
    )

    # --- Factor references ---
    get_factor = None
    for cand in by_caption.get("مقدار عامل در حکم") or []:
        if cand.get("Value") == "GetFactorValue":
            get_factor = cand
            break
    get_statute_factor = None
    for cand in by_caption.get("مقدار قبلی عامل") or []:
        if cand.get("Value") == "GetStatuteFactorValue":
            get_statute_factor = cand
            break
    dyn = None
    for row in by_value.values():
        if row.get("Kind") == "DynamicElements" and row.get("Value") == "GetStatuteFactorsForStatuteCalc":
            dyn = row
            break
    att_dyn = None
    for row in by_value.values():
        if row.get("Kind") == "DynamicElements" and row.get("Value") == "GetAttendanceMonthlyFactorsForOtherCalcs":
            att_dyn = row
            break

    add(
        "BFactor{N}_A / Factor{N}_A (statute-mapped N)",
        (get_factor or {}).get("Value", "GetFactorValue")
        + " + DynamicElements:"
        + (dyn or {}).get("Value", "GetStatuteFactorsForStatuteCalc"),
        (get_factor or {}).get("Caption", "مقدار عامل در حکم"),
        "mapped",
        "Cross-factor amount in same calc → GetFactorValue / dynamic statute factor "
        "element for migrated DestStatuteFactor (via StatuteFactorMigrationMapping).",
        "factor_ref",
    )
    add(
        "BFactor{N}_TM",
        (get_statute_factor or {}).get("Value", "GetStatuteFactorValue"),
        (get_statute_factor or {}).get("Caption", "مقدار قبلی عامل"),
        "partial",
        "Likely month-total / alternate series of factor N. Confirm with payroll expert; "
        "may map to GetStatuteFactorValue or a separate factor.",
        "factor_ref",
    )

    # Per concrete factor id
    for fr in factor_rows:
        fid = fr["SourceFactorID"]
        in_map = fr["InStatuteFactorMap"]
        hint = fr["Hint"]
        name = fr["Name"]
        if in_map:
            add(
                f"BFactor{fid}_A / Factor{fid}_A ({name})",
                "GetFactorValue / GetStatuteFactorsForStatuteCalc",
                f"عامل حکمی ← source {fid}",
                "mapped",
                "Source factor is in StatuteFactorMigrationMapping; use dest StatuteFactorID.",
                "factor_ref_item",
                evidence=f"occurrences={fr['Occurrences']}",
            )
        elif hint.startswith("attendance") or fid in (10, 12014):
            add(
                f"BFactor{fid}_A ({name})",
                (att_dyn or {}).get("Value", "GetAttendanceMonthlyFactorsForOtherCalcs")
                + " / GetSumOfAttendanceFactorInRangeForOtherCalcs",
                (att_dyn or {}).get("Caption", "عوامل کارکرد"),
                "partial",
                "Not a statute factor in our map. Conceptually attendance/work-days; "
                "map via Attendance FormulaContext dynamics after creating/matching "
                "an attendance factor. Until then: leftover for decision.",
                "factor_ref_item",
                evidence=f"occurrences={fr['Occurrences']}; hint={hint}",
            )
        else:
            add(
                f"BFactor{fid}_A ({name})",
                "",
                "",
                "leftover",
                "Referenced in formulas but not in StatuteFactorMigrationMapping "
                "(not on rule-doc detail/score set we migrated). Create as statute "
                "factor or attendance/other factor, then map.",
                "factor_ref_item",
                evidence=f"occurrences={fr['Occurrences']}; hint={hint or 'unknown'}",
            )

    # Literal numeric constants
    add(
        "numeric literals (30, 29, 160, 140, 21990, …)",
        "(literal)",
        "",
        "mapped",
        "Constants carry over unchanged into C# formula body.",
        "literal",
    )

    # Top expression shapes as evidence rows
    for expr, cnt in expr_counter.most_common(30):
        if not expr or expr == "0":
            continue
        add(
            f"EXPR: {expr}",
            "",
            "",
            "shape",
            "Distinct post-split OutputExpr shape (for review; compose from token maps).",
            "expr_shape",
            evidence=f"count={cnt}",
        )

    return mappings


def write_reports(inventory, token_rows, factor_rows, mappings, expr_counter):
    OUT.mkdir(parents=True, exist_ok=True)

    write_csv(
        OUT / "context_inventory.csv",
        inventory,
        ["Kind", "Caption", "Value", "Type", "Description"],
    )
    write_csv(OUT / "source_token_inventory.csv", token_rows, ["TokenPattern", "Occurrences"])
    write_csv(
        OUT / "source_factor_refs.csv",
        factor_rows,
        [
            "SourceFactorID",
            "Occurrences",
            "Name",
            "FieldName",
            "InStatuteFactorMap",
            "Hint",
        ],
    )
    write_csv(
        OUT / "token_mapping.csv",
        mappings,
        [
            "Category",
            "SourceConcept",
            "DestValue",
            "DestCaption",
            "Status",
            "Notes",
            "Evidence",
        ],
    )

    leftovers = [m for m in mappings if m["Status"] in ("leftover", "partial")]
    shapes = [m for m in mappings if m["Status"] == "shape"]
    mapped = [m for m in mappings if m["Status"] == "mapped"]
    eliminated = [m for m in mappings if m["Status"] == "eliminated"]

    # leftovers.md
    lines = [
        "# Formula context mapping — leftovers & partials\n",
        "For decision before implementing C# formula bodies.\n",
        "## Leftovers (no clean 1:1 context token)\n",
    ]
    for m in leftovers:
        if m["Status"] != "leftover":
            continue
        lines.append(f"### `{m['SourceConcept']}`")
        lines.append(f"- Category: {m['Category']}")
        lines.append(f"- Notes: {m['Notes']}")
        if m.get("Evidence"):
            lines.append(f"- Evidence: {m['Evidence']}")
        lines.append("")
    lines.append("## Partials (usable dest API, but not 1:1 / needs confirm)\n")
    for m in leftovers:
        if m["Status"] != "partial":
            continue
        lines.append(f"### `{m['SourceConcept']}`")
        lines.append(f"- Dest: `{m['DestValue']}` ({m['DestCaption']})")
        lines.append(f"- Notes: {m['Notes']}")
        if m.get("Evidence"):
            lines.append(f"- Evidence: {m['Evidence']}")
        lines.append("")

    # Suggested new factors from leftover factor refs
    lines.append("## Suggested factors to create / import (from leftover factor refs)\n")
    lines.append("| Source PfID | Name | Why |")
    lines.append("|-------------|------|-----|")
    for m in mappings:
        if m["Category"] != "factor_ref_item":
            continue
        if m["Status"] in ("leftover", "partial"):
            lines.append(
                f"| {m['SourceConcept']} | — | {m['Status']}: {m['Notes'][:120]} |"
            )
    (OUT / "leftovers.md").write_text("\n".join(lines), encoding="utf-8")

    # summary
    summary = [
        "# FormulaContext ↔ source formula mapping summary\n",
        "## Scope\n",
        "- Dest: `HCM3.FormulaContext` where `[Key]=EmployeeStatuteCalc`",
        "- Source: `PAY_PayrollBackFormula` / `PAY_PfFormula` for mapped statute factors, "
        "plus post-split `OutputExpr` from property migration mapping\n",
        "## Counts\n",
        f"- Context inventory entries: **{len(inventory)}**",
        f"- Mapping rows: **{len(mappings)}** "
        f"(mapped={len(mapped)}, eliminated={len(eliminated)}, "
        f"partial={sum(1 for m in leftovers if m['Status']=='partial')}, "
        f"leftover={sum(1 for m in leftovers if m['Status']=='leftover')}, "
        f"expr_shapes={len(shapes)})",
        f"- Distinct non-zero OutputExpr shapes: **{sum(1 for e,c in expr_counter.items() if e and e!='0')}**\n",
        "## What maps cleanly\n",
        "- Arithmetic `+ - * / ()`",
        "- `CASE/WHEN` → `if / else if / else`",
        "- `round` → `Round`; `convert(numeric,…)` → `ToDecimal`",
        "- `BFactor{N}_A` / `Factor{N}_A` when N is a **migrated statute factor** → "
        "`GetFactorValue` + dynamic `GetStatuteFactorsForStatuteCalc`",
        "- `FxPay_PersonnelKind*` → **eliminated** by ET-specific properties (step 17)\n",
        "## Main decisions needed (see leftovers.md)\n",
        "1. **`Self`** — no context token; define base-amount strategy per factor.",
        "2. **`BFactor10_A` (روزهای کارکرد)** and **`BFactor12014_A`** — attendance-like; "
        "create/match attendance factors then wire via attendance dynamics.",
        "3. **Unmapped payroll factors** referenced in expressions (140,141,150,217,…) — "
        "create as statute factors or exclude from calc.",
        "4. **`FxPay_MonthDayWithPF`** — rare; custom helper vs attendance days.",
        "5. **`BFactor*_TM`** — confirm meaning (month total?) before mapping.\n",
        "## Observation\n",
        "Source formulas for these statute factors are **expression-like** "
        "(Self, factor refs, arithmetic, CASE, round) — not SQL joins. "
        "That matches your read and makes FormulaContext mapping feasible "
        "once leftovers above are decided.\n",
        "## Files\n",
        "- `context_inventory.csv` — full dest vocabulary extract",
        "- `source_token_inventory.csv` / `source_factor_refs.csv`",
        "- `token_mapping.csv` — full mapping table",
        "- `leftovers.md` — decision list",
    ]
    (OUT / "mapping_summary.md").write_text("\n".join(summary), encoding="utf-8")


def main():
    cfg = json.load(open(ROOT / "config.json", encoding="utf-8"))
    src = pyodbc.connect(cfg["source_conn"])
    dst = pyodbc.connect(cfg["dest_conn"])

    print("Loading FormulaContext EmployeeStatuteCalc...")
    inventory, by_value, by_caption, _root = load_context(dst)
    print(f"  inventory entries: {len(inventory)}")

    print("Inventorying source formula tokens...")
    token_rows, factor_rows, expr_counter, _texts, _outputs = inventory_source_tokens(
        src, dst
    )
    print(
        f"  token patterns: {len(token_rows)}, "
        f"factor ids referenced: {len(factor_rows)}, "
        f"distinct OutputExpr: {len(expr_counter)}"
    )

    print("Building mappings...")
    mappings = build_mappings(by_value, by_caption, factor_rows, expr_counter)
    write_reports(inventory, token_rows, factor_rows, mappings, expr_counter)

    status = Counter(m["Status"] for m in mappings)
    print(f"Done. Status counts: {dict(status)}")
    print(f"Outputs: {OUT}")

    src.close()
    dst.close()


if __name__ == "__main__":
    main()
