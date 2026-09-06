"""
Shared statute-factor formula break helpers (date × employment-type slots).

Used by:
  - analysis/formula_et_date_break/analyze_formula_breaks.py (report)
  - steps/step_17_statute_factor.py (property migration)
"""
from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd

# FxPAY_PersonnelKind / Kind2: 6 formula args after personnel id
KIND_FUNCS = {
    "fxpay_personnelkind": "PersonnelKind",
    "dbo.fxpay_personnelkind": "PersonnelKind",
    "fxpay_personnelkind2": "PersonnelKind2",
    "dbo.fxpay_personnelkind2": "PersonnelKind2",
}
ET_FUNCS = {
    "fxpay_personnelemploymenttype": "PersonnelEmploymentType",
    "dbo.fxpay_personnelemploymenttype": "PersonnelEmploymentType",
}
SPECIAL_FUNCS = {
    "fxpay_personnelkindsepecial": "PersonnelKindSepecial",
    "dbo.fxpay_personnelkindsepecial": "PersonnelKindSepecial",
}

# Slot labels (ET membership is dispatcher-specific — see DISPATCHER_SLOT_ETS)
SLOT_META = {
    "F1": {"label": "کارمند رسمی", "notes": "kind=2 AND insurance in (40002,40003)"},
    "F2": {"label": "کارمند غیررسمی", "notes": "kind=2 otherwise (حکمی/پیمانی/قراردادی/…)"},
    "F3": {"label": "کارگر / مسیر Formula3", "notes": "Depends on dispatcher (see DISPATCHER_SLOT_ETS)"},
    "F4": {"label": "خدماتی / انجام کار مشخص / مسیر Formula4", "notes": "Depends on dispatcher"},
    "F5": {"label": "نیروی انتظامی / پیمانی / KindSepecial خاص", "notes": "Depends on dispatcher"},
    "F6": {"label": "هیئت مدیره یا KindSepecial انجام‌کار غیرخاص", "notes": "Depends on dispatcher"},
    "F7": {"label": "KindSepecial arg7 (نیروی انتظامی)", "notes": "See FxPAY_PersonnelKindSepecial"},
    "F8": {"label": "KindSepecial arg8 (هیئت مدیره)", "notes": "Runtime flag Pf 1506"},
    "ALL": {"label": "همه انواع استخدام (بدون انشعاب ET)", "notes": "No ET dispatcher"},
}

# Prefer earlier slots when the same Dest ET is claimed by multiple slots
SLOT_PRIORITY = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "ALL"]

# Base ET membership from UDF bodies (static; date overrides below).
# Derived from OBJECT_DEFINITION of FxPAY_PersonnelKind / Kind2 / EmploymentType / KindSepecial.
DISPATCHER_SLOT_ETS = {
    "PersonnelKind": {
        # parent kind only — EtID 12,13 are parent=1 → Formula3 (NOT Formula4)
        "F1": [22, 23],
        "F2": [20, 21, 24, 25, 26, 38],
        "F3": [1, 10, 11, 12, 13, 14],
        "F4": [3, 30, 31, 32, 33, 35, 36, 37, 39],
        "F5": [],  # kind=4 has no leaf rows in TBL_EmploymentType
        "F6": [],  # هیئت مدیره runtime flag
    },
    "PersonnelKind2": {
        # kind=1 → F3, then logical override: EtID in (12,13) → F4
        "F1": [22, 23],
        "F2": [20, 24, 25, 26, 38],
        "F3": [1, 10, 11, 14],
        "F4": [12, 13],
        "F5": [21, 23],
        "F6": [],
    },
    "PersonnelEmploymentType": {
        # Uses statute EtID; 10,11→F3; 12,13→F4 (EtID 13 date-split below)
        "F1": [22, 23],
        "F2": [20, 21, 24, 25, 26, 38],
        "F3": [10, 11],
        "F4": [12],
        "F5": [],
        "F6": [],
    },
    "PersonnelKindSepecial": {
        # Different arg numbering; F5 needs Pf 12029=1 (runtime) — skipped in expand
        "F1": [22, 23],
        "F2": [20],
        "F3": [21, 23],
        "F4": [10, 11],
        "F5": [],
        "F6": [12, 13],
        "F7": [],
        "F8": [],
    },
}

# Logical date breakpoints inside UDF bodies (not BackFormula month changes).
# Each row: for dispatcher+source_et, use `slot` on [from_yyyymm, to_yyyymm] (inclusive; None=open).
LOGICAL_DATE_SLOT_WINDOWS = [
    {
        "dispatcher": "PersonnelEmploymentType",
        "source_et_id": 13,
        "from_yyyymm": None,
        "to_yyyymm": 139811,
        "slot": "F4",
        "notes": "EtID 13 in (12,13) → Formula4 until before 1398/12/01",
    },
    {
        "dispatcher": "PersonnelEmploymentType",
        "source_et_id": 13,
        "from_yyyymm": 139812,
        "to_yyyymm": None,
        "slot": "F3",
        "notes": "ماده 124: HRS_RdExcuteDate>=1398/12/01 → Formula3",
    },
]


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_shamsi_yyyymm(raw) -> int | None:
    """
    Normalize Shamsi date fragments to YYYYMM.
    Accepts YYYYMM ints, 'YYYY/MM', 'YYYY/MM/DD', or year-only 'YYYY' → YYYY01.
    Rejects junk / out-of-range.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        if 130001 <= n <= 150012:
            mm = n % 100
            if 1 <= mm <= 12:
                return n
        if 1300 <= n <= 1500:
            return n * 100 + 1
        return month_id_to_yyyymm(n)

    text = str(raw).strip()
    if not text or text in ("0", "____/__/__", "/  /", "//"):
        return None
    # YYYY/MM or YYYY/MM/DD
    if "/" in text:
        parts = text.replace("-", "/").split("/")
        try:
            y = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 1
        except ValueError:
            return None
        if m < 1 or m > 12:
            m = 1
        if 1300 <= y <= 1500:
            return y * 100 + m
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        n = int(digits[:6] if len(digits) >= 6 else digits)
    except ValueError:
        return None
    return normalize_shamsi_yyyymm(n)


def month_id_to_yyyymm(month_id) -> int | None:
    """Convert PAY_MonthID (YYMM or YYYYMM) to Shamsi YYYYMM."""
    if month_id is None or (isinstance(month_id, float) and pd.isna(month_id)):
        return None
    mid = int(month_id)
    if mid >= 130001:
        mm = mid % 100
        if 1 <= mm <= 12:
            return mid
    if mid >= 130000:  # e.g. 130000 junk
        return None
    if mid <= 0:
        return None
    yy, mm = divmod(mid, 100)
    if mm < 1 or mm > 12:
        # year-only month id
        if 50 <= mid <= 99:
            return (1300 + mid) * 100 + 1
        if 1300 <= mid <= 1500:
            return mid * 100 + 1
        return None
    return (1300 + yy) * 100 + mm


def split_args(arg_str: str) -> list[str]:
    args = []
    buf = []
    depth = 0
    in_str = None
    i = 0
    s = arg_str
    while i < len(s):
        ch = s[i]
        if in_str:
            buf.append(ch)
            if ch == in_str and s[i - 1] != "\\":
                in_str = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append(norm_ws("".join(buf)))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = norm_ws("".join(buf))
    if tail:
        args.append(tail)
    return args


def find_dispatcher_calls(sql: str):
    if not sql:
        return []
    lower = sql.lower()
    names = sorted(
        set(list(KIND_FUNCS) + list(ET_FUNCS) + list(SPECIAL_FUNCS)),
        key=len,
        reverse=True,
    )
    calls = []
    occupied = []  # (start, end) spans already claimed by a longer/earlier match

    def _overlaps(a0, a1):
        for b0, b1 in occupied:
            if a0 < b1 and b0 < a1:
                return True
        return False

    for name in names:
        start = 0
        while True:
            idx = lower.find(name, start)
            if idx < 0:
                break
            if idx > 0 and (lower[idx - 1].isalnum() or lower[idx - 1] == "_"):
                start = idx + 1
                continue
            after = idx + len(name)
            while after < len(sql) and sql[after].isspace():
                after += 1
            if after >= len(sql) or sql[after] != "(":
                start = idx + 1
                continue
            depth = 0
            j = after
            in_str = None
            while j < len(sql):
                ch = sql[j]
                if in_str:
                    if ch == in_str and sql[j - 1] != "\\":
                        in_str = None
                    j += 1
                    continue
                if ch in ("'", '"'):
                    in_str = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        if not _overlaps(idx, end):
                            arg_blob = sql[after + 1 : j]
                            key = name
                            if key in KIND_FUNCS:
                                variant = KIND_FUNCS[key]
                            elif key in ET_FUNCS:
                                variant = ET_FUNCS[key]
                            else:
                                variant = SPECIAL_FUNCS[key]
                            calls.append(
                                {
                                    "func_key": key,
                                "variant": variant,
                                    "args": split_args(arg_blob),
                                "start": idx,
                                    "end": end,
                            }
                        )
                            occupied.append((idx, end))
                        break
                j += 1
            start = idx + 1
    calls.sort(key=lambda c: c["start"])
    return calls


def extract_slots_from_call(call) -> tuple[dict[str, str], str]:
    variant = call["variant"]
    args = call["args"]
    slots = {}
    if variant in ("PersonnelKind", "PersonnelKind2"):
        if len(args) < 7:
            return {}, f"expected >=7 args for {variant}, got {len(args)}"
        for i in range(1, 7):
            slots[f"F{i}"] = args[i]
        return slots, "ok"
    if variant == "PersonnelEmploymentType":
        if len(args) < 8:
            return {}, f"expected >=8 args for EmploymentType, got {len(args)}"
        for i in range(2, 8):
            slots[f"F{i - 1}"] = args[i]
        return slots, "ok"
    if variant == "PersonnelKindSepecial":
        if len(args) < 9:
            return {}, f"expected >=9 args for KindSepecial, got {len(args)}"
        for i in range(1, 9):
            slots[f"F{i}"] = args[i]
        return slots, "ok"
    return {}, f"unknown variant {variant}"


def is_trivial_zero(expr: str) -> bool:
    e = norm_ws(expr).lower()
    return e in ("0", "0.0", "null", "")


def load_et_maps(dest_cnxn):
    try:
        df = pd.read_sql(
            """
            SELECT SourceEmploymentTypeID, DestEmploymentTypeID
            FROM master.dbo.EmploymentTypeMigrationMapping
            """,
            dest_cnxn,
        )
    except Exception:
        return {}
    return {
        int(r["SourceEmploymentTypeID"]): int(r["DestEmploymentTypeID"])
        for _, r in df.iterrows()
    }


def source_ets_for_slot(slot: str, dispatcher: str | None) -> list[int]:
    """
    Base (non-date) source ET IDs for a slot under a dispatcher UDF body.
    Date-scoped ETs (e.g. EmploymentType EtID 13) are handled via
    LOGICAL_DATE_SLOT_WINDOWS / et_slot_windows — not listed here for both slots.
    """
    if not slot or slot == "ALL":
        return []
    table = DISPATCHER_SLOT_ETS.get(dispatcher or "") or {}
    if table:
        return list(table.get(slot) or [])
    # Unknown dispatcher: empty (do not invent Kind membership)
    return []


def dest_ids_for_slot(slot: str, et_map: dict, dispatcher: str | None = None) -> list[int]:
    out = []
    for sid in source_ets_for_slot(slot, dispatcher):
        did = et_map.get(int(sid))
        if did is not None:
            out.append(did)
    return out


def _yyyymm_le(a, b) -> bool:
    if a is None or b is None:
        return True
    return int(a) <= int(b)


def _intersect_month_range(a_from, a_to, b_from, b_to):
    """
    Intersect two inclusive YYYYMM ranges (None = open).
    Returns (start, end) or None if empty.
    """
    start = a_from if b_from is None else (b_from if a_from is None else max(int(a_from), int(b_from)))
    end_candidates = [x for x in (a_to, b_to) if x is not None]
    end = min(int(x) for x in end_candidates) if end_candidates else None
    if start is not None and end is not None and int(start) > int(end):
        return None
    return (start, end)


def et_slot_windows(dispatcher: str | None, source_et_id: int) -> list[tuple]:
    """
    Return [(from_yyyymm|None, to_yyyymm|None, slot, note)] covering how a source
    ET maps to formula slots over time for this dispatcher (UDF logical rules).
    """
    if not dispatcher:
        return []
    dated = [
        w
        for w in LOGICAL_DATE_SLOT_WINDOWS
        if w["dispatcher"] == dispatcher and int(w["source_et_id"]) == int(source_et_id)
    ]
    if dated:
        return [
            (w["from_yyyymm"], w["to_yyyymm"], w["slot"], w.get("notes") or "")
            for w in dated
        ]
    table = DISPATCHER_SLOT_ETS.get(dispatcher) or {}
    for slot, ets in table.items():
        if int(source_et_id) in {int(x) for x in ets}:
            return [(None, None, slot, "")]
    return []


def should_skip_slot(slot: str | None, dispatcher: str | None) -> bool:
    """Slots that are runtime flags / unused for ET-keyed properties."""
    if not slot:
        return True
    if slot in ("F7", "F8"):
        return True
    if slot == "F6" and dispatcher != "PersonnelKindSepecial":
        return True  # هیئت مدیره flag for Kind/Kind2/EmploymentType
    if slot == "F5" and dispatcher == "PersonnelKindSepecial":
        return True  # needs Pf 12029=1 at runtime
    if slot == "F5" and dispatcher in (None, "PersonnelKind", "PersonnelEmploymentType"):
        return True  # no mapped leaf ETs
    return False


def analyze_version(formula_sql: str) -> dict:
    sql = formula_sql or ""
    calls = find_dispatcher_calls(sql)
    if not calls:
        return {
            "parse_status": "no_et_dispatcher",
            "dispatcher": None,
            "slots": {"ALL": norm_ws(sql)},
            "outer_wrapper": None,
            "notes": "No FxPAY_PersonnelKind / EmploymentType / Kind2 / KindSepecial call",
        }

    primary = calls[0]
    slots, note = extract_slots_from_call(primary)
    if not slots:
        return {
            "parse_status": "parse_error",
            "dispatcher": primary["variant"],
            "slots": {},
            "outer_wrapper": None,
            "notes": note,
        }

    outer = None
    span = sql[primary["start"] : primary["end"]]
    if norm_ws(sql) != norm_ws(span):
        outer = norm_ws(sql)

    multi = ""
    if len(calls) > 1:
        multi = f"; {len(calls)} dispatcher calls (using first)"

    return {
        "parse_status": "ok" if note == "ok" else note,
        "dispatcher": primary["variant"],
        "slots": slots,
        "outer_wrapper": outer,
        "notes": note + multi,
    }


def collapse_versions(version_rows: list[dict]) -> list[dict]:
    """
    Per factor, keep only versions where (dispatcher + slot signature) changes.
    Expand each kept version to one row per slot.
    """
    by_factor = defaultdict(list)
    for r in version_rows:
        by_factor[r["SourcePayrollFactorID"]].append(r)

    collapsed = []
    for pf, items in sorted(by_factor.items()):
        items = sorted(
            items,
            key=lambda x: (
                x["IssueYearMonth"] is None,
                x["IssueYearMonth"] or 0,
                x["SourceBackFormulaID"] or 0,
            ),
        )
        kept = []
        prev_sig = None
        for r in items:
            slot_items = tuple(
                sorted(
                    (k, norm_ws(v).lower())
                    for k, v in (r.get("slots") or {}).items()
                )
            )
            sig = (r.get("dispatcher"), slot_items, r.get("parse_status"))
            if sig == prev_sig and kept:
                kept[-1]["covered"].append(r.get("SourceMonthID"))
                kept[-1]["EffectiveToMonth"] = r.get("IssueYearMonth")
                continue
            prev_sig = sig
            if kept:
                cur = r.get("IssueYearMonth")
                prev_from = kept[-1]["EffectiveFromMonth"]
                if prev_from and cur and cur > prev_from:
                    y, m = divmod(int(cur), 100)
                    if m == 1:
                        kept[-1]["EffectiveToMonth"] = (y - 1) * 100 + 12
                    else:
                        kept[-1]["EffectiveToMonth"] = y * 100 + (m - 1)
            kept.append(
                {
                    **r,
                    "EffectiveFromMonth": r.get("IssueYearMonth"),
                    "EffectiveToMonth": None,
                    "covered": [r.get("SourceMonthID")],
                }
            )

        for ver in kept:
            covered = [c for c in ver["covered"] if c is not None]
            covered_str = ",".join(str(c) for c in covered)
            slots = ver.get("slots") or {}
            if not slots:
                collapsed.append(
                    {
                        **{k: v for k, v in ver.items() if k not in ("slots", "covered")},
                        "SlotCode": None,
                        "OutputExpr": None,
                        "IsTrivialZero": False,
                        "SourceMonthIDsCovered": covered_str,
                    }
                )
                continue
            for slot, expr in slots.items():
                collapsed.append(
                    {
                        **{k: v for k, v in ver.items() if k not in ("slots", "covered")},
                        "SlotCode": slot,
                        "OutputExpr": expr,
                        "IsTrivialZero": is_trivial_zero(expr),
                        "SourceMonthIDsCovered": covered_str,
                    }
                )
    return collapsed


def _earliest_detail_usage_year_months(source_cnxn, factor_ids) -> dict[int, int]:
    """
    Earliest Shamsi YYYYMM a factor appears on a حکم detail line.
    Uses MIN of valid HRS_RddRegisterDate and parent HRS_RdExcuteDate / ExportDate.
    """
    if not factor_ids:
        return {}
    id_list = ",".join(str(int(x)) for x in factor_ids)
    df = pd.read_sql(
        f"""
        SELECT
            d.PAY_PfID_fk AS SourcePayrollFactorID,
            MIN(TRY_CAST(
                REPLACE(LEFT(LTRIM(RTRIM(d.HRS_RddRegisterDate)), 7), N'/', N'')
                AS INT
            )) AS MinRegisterYM,
            MIN(TRY_CAST(
                REPLACE(LEFT(LTRIM(RTRIM(rd.HRS_RdExcuteDate)), 7), N'/', N'')
                AS INT
            )) AS MinExecuteYM,
            MIN(TRY_CAST(
                REPLACE(LEFT(LTRIM(RTRIM(rd.HRS_RdExportDate)), 7), N'/', N'')
                AS INT
            )) AS MinExportYM
        FROM dbo.HRS_RuleDocumentDetail d
        INNER JOIN dbo.HRS_RuleDocument rd ON rd.HRS_RdID = d.HRS_RdID_fk
        WHERE d.PAY_PfID_fk IN ({id_list})
        GROUP BY d.PAY_PfID_fk
        """,
        source_cnxn,
    )
    out = {}
    for _, r in df.iterrows():
        cands = []
        for col in ("MinRegisterYM", "MinExecuteYM", "MinExportYM"):
            ym = normalize_shamsi_yyyymm(r[col])
            if ym is not None:
                cands.append(ym)
        if cands:
            out[int(r["SourcePayrollFactorID"])] = min(cands)
    return out


def _factor_register_year_months(source_cnxn, factor_ids) -> dict[int, int]:
    if not factor_ids:
        return {}
    id_list = ",".join(str(int(x)) for x in factor_ids)
    df = pd.read_sql(
        f"""
        SELECT PAY_PfID AS SourcePayrollFactorID,
               PAY_PfRegisetrDate AS RegDate
        FROM dbo.PAY_PayrollFactor
        WHERE PAY_PfID IN ({id_list})
        """,
        source_cnxn,
    )
    out = {}
    for _, r in df.iterrows():
        ym = normalize_shamsi_yyyymm(r["RegDate"])
        if ym is not None:
            out[int(r["SourcePayrollFactorID"])] = ym
    return out


def factor_anchor_year_months(source_cnxn, factor_ids) -> dict[int, int]:
    """
    Precise property-start candidate per factor:
    MIN(factor register, earliest detail usage on حکم).
    """
    usage = _earliest_detail_usage_year_months(source_cnxn, factor_ids)
    register = _factor_register_year_months(source_cnxn, factor_ids)
    out = {}
    for pf in {int(x) for x in factor_ids}:
        cands = [x for x in (usage.get(pf), register.get(pf)) if x is not None]
        if cands:
            out[pf] = min(cands)
    return out


def load_formula_versions(source_cnxn, factor_ids):
    """
    Load BackFormula (+ PfFormula fallback) versions for the given source factor IDs.
    IssueYearMonth is always Shamsi YYYYMM.

    Dating rules (precise):
      - BackFormula rows: PAY_MonthID_fk → YYYYMM (formula change-points)
      - First version for a factor is anchored back to MIN(factor register,
        earliest Detail/RD execute|export) when that is earlier than the first
        BackFormula month (so properties cover full حکم usage history)
      - No BackFormula: PfFormula at that same anchor month
      - No formula text at all: stub `0` at anchor (property shell for expert)
    """
    if not factor_ids:
        return [], {}

    ids = [int(x) for x in factor_ids]
    id_list = ",".join(str(x) for x in ids)
    anchors = factor_anchor_year_months(source_cnxn, ids)

    names = pd.read_sql(
        f"""
        SELECT PAY_PfID, PAY_PfName, PAY_PfFieldName, PAY_PfFormula
        FROM dbo.PAY_PayrollFactor
        WHERE PAY_PfID IN ({id_list})
        """,
        source_cnxn,
    )
    name_map = {
        int(r["PAY_PfID"]): (
            str(r["PAY_PfName"] or "").strip(),
            str(r["PAY_PfFieldName"] or "").strip(),
            r["PAY_PfFormula"],
        )
        for _, r in names.iterrows()
    }

    back = pd.read_sql(
        f"""
        SELECT
            PAY_PbfID AS SourceBackFormulaID,
            PAY_PfID_fk AS SourcePayrollFactorID,
            PAY_MonthID_fk AS SourceMonthID,
            PAY_PbfFormula AS FormulaSql
        FROM dbo.PAY_PayrollBackFormula
        WHERE ISNULL(PAY_PbfActive, 1) = 1
          AND PAY_PfID_fk IN ({id_list})
          AND PAY_MonthID_fk IS NOT NULL
          AND PAY_MonthID_fk > 0
          AND NULLIF(LTRIM(RTRIM(PAY_PbfFormula)), '') IS NOT NULL
        ORDER BY PAY_PfID_fk, PAY_MonthID_fk, PAY_PbfID
        """,
        source_cnxn,
    )

    rows = []
    factors_with_back = set()
    first_bf_month: dict[int, int] = {}
    for _, r in back.iterrows():
        pf = int(r["SourcePayrollFactorID"])
        factors_with_back.add(pf)
        ym = month_id_to_yyyymm(r["SourceMonthID"])
        if ym is None:
            continue
        if pf not in first_bf_month or ym < first_bf_month[pf]:
            first_bf_month[pf] = ym
        rows.append(
            {
                "SourcePayrollFactorID": pf,
                "SourceBackFormulaID": int(r["SourceBackFormulaID"]),
                "SourceMonthID": int(r["SourceMonthID"]),
                "IssueYearMonth": ym,
                "FormulaSql": str(r["FormulaSql"]),
                "SourceKind": "PAY_PayrollBackFormula",
            }
        )

    # Anchor first BackFormula version earlier when Detail usage / Pf register is older
    by_pf_rows = defaultdict(list)
    for row in rows:
        by_pf_rows[row["SourcePayrollFactorID"]].append(row)
    for pf, pf_rows in by_pf_rows.items():
        anchor = anchors.get(pf)
        if not anchor:
            continue
        pf_rows.sort(
            key=lambda x: (x["IssueYearMonth"] or 0, x["SourceBackFormulaID"] or 0)
        )
        first = pf_rows[0]
        if first["IssueYearMonth"] and int(first["IssueYearMonth"]) > int(anchor):
            first["IssueYearMonth"] = int(anchor)
            first["SourceKind"] = (
                str(first.get("SourceKind") or "") + "+anchored_detail_usage"
            )

    missing = [i for i in ids if i not in factors_with_back]
    if missing:
        miss_list = ",".join(str(x) for x in missing)
        pf_df = pd.read_sql(
            f"""
            SELECT PAY_PfID AS SourcePayrollFactorID, PAY_PfFormula AS FormulaSql
            FROM dbo.PAY_PayrollFactor
            WHERE PAY_PfID IN ({miss_list})
            """,
            source_cnxn,
        )
        for _, row in pf_df.iterrows():
            source_id = int(row["SourcePayrollFactorID"])
            sql = (str(row["FormulaSql"]).strip() if pd.notna(row["FormulaSql"]) else "")
            month_id = anchors.get(source_id)
            if month_id is None:
                # last resort: leave None (will be skipped later) — still emit for diagnosis
                month_id = None
            if not sql:
                sql = "0"
                kind = "stub_no_formula+detail_anchor"
            else:
                kind = "PAY_PayrollFactor.PAY_PfFormula+detail_anchor"
            rows.append(
                {
                    "SourcePayrollFactorID": source_id,
                    "SourceBackFormulaID": None,
                    "SourceMonthID": None,
                    "IssueYearMonth": int(month_id) if month_id else None,
                    "FormulaSql": sql,
                    "SourceKind": kind,
                }
            )

    factor_meta = {
        pf: {
            "FactorName": name_map.get(pf, ("", "", None))[0],
            "FieldName": name_map.get(pf, ("", "", None))[1],
            "AnchorYearMonth": anchors.get(pf),
        }
        for pf in ids
    }
    return rows, factor_meta


def build_collapsed_breaks(source_cnxn, dest_cnxn, factor_ids):
    """
    Analyze + collapse formula versions for factor_ids.
    Returns list of slot-level break dicts (not yet expanded to dest ET rows).
    """
    et_map = load_et_maps(dest_cnxn)
    versions, factor_meta = load_formula_versions(source_cnxn, factor_ids)

    analyzed = []
    for v in versions:
        analysis = analyze_version(v["FormulaSql"])
        meta = factor_meta.get(v["SourcePayrollFactorID"]) or {}
        analyzed.append(
            {
            **v,
            **meta,
            "dispatcher": analysis["dispatcher"],
            "parse_status": analysis["parse_status"],
            "outer_wrapper": analysis["outer_wrapper"],
            "notes": analysis["notes"],
            "slots": analysis["slots"],
        }
        )

    collapsed = collapse_versions(analyzed)
    enriched = []
    for r in collapsed:
        slot = r.get("SlotCode")
        dispatcher = r.get("dispatcher")
        meta = SLOT_META.get(slot or "", {})
        src_ets = source_ets_for_slot(slot, dispatcher) if slot else []
        # Include ETs that land on this slot only via logical date windows
        for w in LOGICAL_DATE_SLOT_WINDOWS:
            if w["dispatcher"] == dispatcher and w["slot"] == slot:
                sid = int(w["source_et_id"])
                if sid not in src_ets:
                    src_ets.append(sid)
            if slot == "ALL":
                src_ets = sorted(et_map.keys())
            dest_ets = sorted(set(et_map.values()))
        else:
            dest_ets = []
            for sid in src_ets:
                did = et_map.get(int(sid))
                if did is not None and did not in dest_ets:
                    dest_ets.append(did)
        enriched.append(
            {
                **r,
                    "SlotLabel": meta.get("label"),
                    "SourceEmploymentTypeIDs": ",".join(str(x) for x in src_ets),
                    "DestEmploymentTypeIDs": ",".join(str(x) for x in dest_ets),
                "_dest_et_list": dest_ets,
                "_src_et_list": src_ets,
            }
        )
    return enriched, et_map


def expand_breaks_to_property_targets(
    break_rows,
    et_map,
    *,
    skip_trivial_zero=True,
    skip_slots_without_et=None,
):
    """
    Expand collapsed slot breaks into unique
    (SourcePayrollFactorID, EffectiveFromMonth, DestEmploymentTypeID) targets.

    Breakpoints are the union of:
      1) BackFormula / PfFormula text change months (version EffectiveFromMonth)
      2) UDF logical date windows (e.g. EmploymentType EtID 13 → F3 from 139812)

    When multiple slots claim the same (from-month, dest ET), prefer non-zero
    expressions and earlier SLOT_PRIORITY.
    """
    del skip_slots_without_et  # superseded by should_skip_slot()
    all_dest_ets = sorted(set(et_map.values()))
    inv_et = {int(s): int(d) for s, d in et_map.items()}

    # Group slot rows of one formula version: (pf, version_from) → rows
    groups = defaultdict(list)
    for r in break_rows:
        pf = r.get("SourcePayrollFactorID")
        frm = r.get("EffectiveFromMonth")
        if pf is None:
            continue
        groups[(int(pf), frm)].append(r)

    targets = []
    skipped_zero = 0
    skipped_no_et = 0
    skipped_conflict = 0
    logical_splits = 0

    for (pf, frm), rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1] or 0)):
        sample = rows[0]
        dispatcher = sample.get("dispatcher")
        v_from = frm
        v_to = sample.get("EffectiveToMonth")
        slot_expr = {}
        slot_meta_row = {}
        for r in rows:
            slot = r.get("SlotCode")
            if slot:
                slot_expr[slot] = r.get("OutputExpr") or ""
                slot_meta_row[slot] = r

        candidates = []

        if "ALL" in slot_expr or (
            not dispatcher and any(r.get("SlotCode") == "ALL" for r in rows)
        ):
            expr = slot_expr.get("ALL") or (rows[0].get("OutputExpr") or "")
            trivial = is_trivial_zero(expr)
            # Keep ALL even when expr is 0 — marks a versioned stub for every ET
            base = slot_meta_row.get("ALL") or sample
            for dest_et in all_dest_ets:
                candidates.append(
                    {
                        "SourcePayrollFactorID": pf,
                        "EffectiveFromMonth": v_from,
                        "EffectiveToMonth": v_to,
                        "DestEmploymentTypeID": int(dest_et),
                        "SlotCode": "ALL",
                        "SlotLabel": SLOT_META["ALL"]["label"],
                        "OutputExpr": expr,
                        "IsTrivialZero": trivial,
                        "dispatcher": dispatcher,
                        "SourceBackFormulaID": base.get("SourceBackFormulaID"),
                        "SourceMonthID": base.get("SourceMonthID"),
                        "SourceKind": base.get("SourceKind"),
                        "FormulaSql": base.get("FormulaSql"),
                        "outer_wrapper": base.get("outer_wrapper"),
                        "parse_status": base.get("parse_status"),
                        "notes": base.get("notes"),
                        "FactorName": base.get("FactorName"),
                        "_prio": SLOT_PRIORITY.index("ALL"),
                        "_trivial": 1 if trivial else 0,
                    }
                )
        else:
            # Walk each mapped source ET through UDF logical slot windows
            for source_et, dest_et in sorted(inv_et.items()):
                windows = et_slot_windows(dispatcher, source_et)
                if not windows:
                    continue
                for w_from, w_to, slot, w_note in windows:
                    if should_skip_slot(slot, dispatcher):
                        skipped_no_et += 1
                        continue
                    inter = _intersect_month_range(v_from, v_to, w_from, w_to)
                    if inter is None:
                        continue
                    prop_from, prop_to = inter
                    # Property starts at intersection start; if open-start, use version from
                    if prop_from is None:
                        prop_from = v_from
                    if prop_from is None:
                        skipped_no_et += 1
                        continue

                    expr = slot_expr.get(slot)
                    if expr is None:
                        skipped_no_et += 1
                        continue
                    trivial = is_trivial_zero(expr)
                    if skip_trivial_zero and trivial:
                        skipped_zero += 1
                        continue

                    base = slot_meta_row.get(slot) or sample
                    notes = base.get("notes") or ""
                    if w_note:
                        notes = (notes + "; " if notes else "") + "logical:" + w_note
                    # Count when a UDF date window creates a property start inside a version
                    if (
                        w_from is not None
                        and v_from is not None
                        and int(prop_from) == int(w_from)
                        and int(w_from) != int(v_from)
                    ):
                        logical_splits += 1

                    prio = SLOT_PRIORITY.index(slot) if slot in SLOT_PRIORITY else 99
                    candidates.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "EffectiveFromMonth": int(prop_from),
                            "EffectiveToMonth": prop_to,
                            "DestEmploymentTypeID": int(dest_et),
                            "SlotCode": slot,
                            "SlotLabel": (SLOT_META.get(slot) or {}).get("label"),
                            "OutputExpr": expr,
                            "IsTrivialZero": trivial,
                            "dispatcher": dispatcher,
                            "SourceBackFormulaID": base.get("SourceBackFormulaID"),
                            "SourceMonthID": base.get("SourceMonthID"),
                            "SourceKind": base.get("SourceKind"),
                            "FormulaSql": base.get("FormulaSql"),
                            "outer_wrapper": base.get("outer_wrapper"),
                            "parse_status": base.get("parse_status"),
                            "notes": notes,
                            "FactorName": base.get("FactorName"),
                            "_prio": prio,
                            "_trivial": 1 if trivial else 0,
                        }
                    )

        # Resolve conflicts per (EffectiveFromMonth, DestET)
        candidates.sort(
            key=lambda c: (
                c["EffectiveFromMonth"] or 0,
                c["DestEmploymentTypeID"],
                c["_trivial"],
                c["_prio"],
                c["SlotCode"] or "",
            )
        )
        claimed = {}
        for c in candidates:
            key = (c["EffectiveFromMonth"], c["DestEmploymentTypeID"])
            if key in claimed:
                skipped_conflict += 1
                continue
            claimed[key] = c
            targets.append({k: v for k, v in c.items() if not k.startswith("_")})

    return targets, {
        "skipped_zero": skipped_zero,
        "skipped_no_et": skipped_no_et,
        "skipped_conflict": skipped_conflict,
        "logical_splits": logical_splits,
        "targets": len(targets),
    }


_FACTOR_REF_RE = re.compile(r"\b[BR]?Factor(\d+)_(A|TM)\b", re.I)
_UDF_RE = re.compile(r"\b((?:dbo\.)?Fx[A-Za-z0-9_]+)\s*\(", re.I)
_SELF_RE = re.compile(r"\bSelf\b", re.I)


def load_rule_document_factor_ids(source_cnxn) -> set[int]:
    """
    Factor IDs that appear on حکم detail lines (HRS_RuleDocumentDetail).
    Scores-only factors are excluded from the statute-factor catalog.
    """
    df = pd.read_sql(
        """
        SELECT DISTINCT PAY_PfID_fk AS PfID
        FROM dbo.HRS_RuleDocumentDetail
        WHERE PAY_PfID_fk IS NOT NULL AND PAY_PfID_fk > 0
        """,
        source_cnxn,
    )
    return {int(x) for x in df["PfID"].tolist()}


def classify_statute_expression(expr: str, rd_factor_ids: set[int]) -> dict:
    """
    Classify a post-split OutputExpr for statute-calc purity.

    Statute-calc (Rahkaran Staff) may depend on: this statute, other *rule-document*
    statute factors, and employee/statute context tokens (Self is noted but not
    resolved here). References to non-RD factors (e.g. attendance days) or payroll
    UDFs are deferred — no guessing / rewriting.

    Returns dict:
      StatutePure (bool): no non-RD factor refs and no blocking UDFs
      HasSelf, HasUdf
      RdFactorRefs, NonRdFactorRefs (sorted unique ints)
      Udfs (list)
      DeferredReason (str|None)
    """
    text = expr or ""
    refs = _FACTOR_REF_RE.findall(text)
    rd_refs = sorted({int(i) for i, _sfx in refs if int(i) in rd_factor_ids})
    non_rd = sorted({int(i) for i, _sfx in refs if int(i) not in rd_factor_ids})
    udfs = sorted({m.lower() for m in _UDF_RE.findall(text)})
    # PersonnelKind already eliminated by ET split; any remaining Fx* is deferred
    blocking_udfs = [u for u in udfs if "personnelkind" not in u.replace("_", "")]
    has_self = bool(_SELF_RE.search(text))

    reasons = []
    if non_rd:
        reasons.append(
            "non_rule_document_factor_refs=" + ",".join(str(x) for x in non_rd)
        )
    if blocking_udfs:
        reasons.append("udfs=" + ",".join(blocking_udfs))

    pure = not non_rd and not blocking_udfs
    return {
        "StatutePure": pure,
        "HasSelf": has_self,
        "HasUdf": bool(blocking_udfs),
        "RdFactorRefs": rd_refs,
        "NonRdFactorRefs": non_rd,
        "Udfs": blocking_udfs,
        "DeferredReason": "; ".join(reasons) if reasons else None,
    }


def annotate_targets_with_statute_filter(targets: list[dict], rd_factor_ids: set[int]):
    """Attach classification fields onto property targets (in place). Returns stats."""
    stats = {
        "pure": 0,
        "deferred_non_rd": 0,
        "deferred_udf": 0,
        "with_self": 0,
    }
    for t in targets:
        cls = classify_statute_expression(t.get("OutputExpr") or "", rd_factor_ids)
        t["StatutePure"] = cls["StatutePure"]
        t["HasSelf"] = cls["HasSelf"]
        t["NonRdFactorRefs"] = ",".join(str(x) for x in cls["NonRdFactorRefs"])
        t["RdFactorRefs"] = ",".join(str(x) for x in cls["RdFactorRefs"])
        t["DeferredReason"] = cls["DeferredReason"]
        if cls["HasSelf"]:
            stats["with_self"] += 1
        if cls["StatutePure"]:
            stats["pure"] += 1
        else:
            if cls["NonRdFactorRefs"]:
                stats["deferred_non_rd"] += 1
            if cls["HasUdf"]:
                stats["deferred_udf"] += 1
    return stats
