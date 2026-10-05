"""
Statute-factor property targets from PAY_PfRollFormula + UDF bodies.

Catalog: PAY_PayrollFactor children of parent 2 (عوامل حکمی) and parent 22
(امتیازات فردی), plus سابقه خدمت leaves that have a user amount on
HRS_RuleDocumentScores. Those service factors have no roll formula;
their property is one shell per employment type.
Formulas: PAY_PfRollFormula only (not PAY_PfFormula / BackFormula).

Dispatchers handled:
  - FxPAY_PersonnelEmploymentType → ET slots (+ EtID 13 date override)
  - FxHRS_RuleDocumentPersonnelCMSL → date windows inside UDF for this PfID
  - FxHRS_RuleDocumentPersonnel / other Fx* → single (or date-split) shell
"""
from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd

from utils.formula_break import (
    DISPATCHER_SLOT_ETS,
    LOGICAL_DATE_SLOT_WINDOWS,
    SLOT_META,
    et_slot_windows,
    factor_anchor_year_months,
    find_dispatcher_calls,
    is_trivial_zero,
    load_et_maps,
    normalize_shamsi_yyyymm,
    should_skip_slot,
    split_args,
)

STATUTE_PARENT_ID = 2
# امتیازات فردی. Same statute-factor path as عوامل حکمی; TypeCode is فرعی.
SCORE_PARENT_ID = 22
STATUTE_PARENT_IDS = (STATUTE_PARENT_ID, SCORE_PARENT_ID)
SERVICE_HISTORY_NOTE = (
    "FxHRS_RuleDocumentPersonnelScoresCMSL reads "
    "HRS_RuleDocumentScores.HRS_RdsUserAmount for this factor "
    "when it builds امتیاز سنوات and امتیاز تجربه."
)

_UDF_CALL_RE = re.compile(r"\b(?:dbo\.)?(Fx[A-Za-z0-9_]+)\s*\(", re.I)
_CMSL_PF_IF_RE = re.compile(
    r"if\s+@PAY_PfID_fk\s*=\s*(\d+)\b",
    re.I,
)
_DATE_CMP_RE = re.compile(
    r"@HRS_RdExcuteDate\s*(>=|<=|>|<|=)\s*'(\d{4}/\d{2}/\d{2})'",
    re.I,
)
_SHAMSI_DATE_RE = re.compile(r"'(\d{4}/\d{2}/\d{2})'")


def load_service_history_factor_ids(source_cnxn) -> set[int]:
    """
    سابقه خدمت leaves that feed the حکم score.
    A factor qualifies when a score-sheet row stores a non-zero user amount
    and the factor is not itself a parent-2 or parent-22 statute factor.
    """
    df = pd.read_sql(
        f"""
        SELECT DISTINCT s.PAY_PfID_fk AS PfID
        FROM dbo.HRS_RuleDocumentScores s
        INNER JOIN dbo.PAY_PayrollFactor pf ON pf.PAY_PfID = s.PAY_PfID_fk
        WHERE pf.PAY_PfID > 0
          AND ISNULL(pf.PAY_PfParentID_fk, 0) NOT IN ({STATUTE_PARENT_ID}, {SCORE_PARENT_ID})
          AND ISNULL(TRY_CAST(s.HRS_RdsUserAmount AS float), 0) <> 0
        """,
        source_cnxn,
    )
    if df.empty:
        return set()
    return {int(x) for x in df["PfID"].tolist()}


def load_parent2_factors(source_cnxn) -> pd.DataFrame:
    service_ids = load_service_history_factor_ids(source_cnxn)
    service_clause = ""
    if service_ids:
        service_clause = (
            " OR PAY_PfID IN (" + ",".join(str(i) for i in sorted(service_ids)) + ")"
        )
    frame = pd.read_sql(
        f"""
        SELECT
            PAY_PfID AS SourcePayrollFactorID,
            PAY_PfID AS PAY_PfID,
            PAY_PfName AS FactorName,
            PAY_PfName AS PAY_PfTitle,
            PAY_PfNote AS FactorNote,
            PAY_PfFieldName AS FieldName,
            PAY_PfParentID_fk AS ParentFactorID,
            PAY_PfInsuranceStatus AS PAY_PfInsuranceStatus,
            PAY_PfActive AS PAY_PfActive,
            PAY_PfRegisetrDate AS FactorRegisterDate,
            CAST(PAY_PfRollFormula AS nvarchar(max)) AS RollFormula,
            CAST(PAY_PfFormula AS nvarchar(max)) AS PfFormula,
            CASE
                WHEN ISNULL(PAY_PfParentID_fk, 0) NOT IN ({STATUTE_PARENT_ID}, {SCORE_PARENT_ID})
                THEN 1 ELSE 0
            END AS IsServiceHistory
        FROM dbo.PAY_PayrollFactor
        WHERE PAY_PfID > 0
          AND (
                PAY_PfParentID_fk IN ({",".join(str(i) for i in STATUTE_PARENT_IDS)})
                {service_clause}
          )
        ORDER BY PAY_PfID
        """,
        source_cnxn,
    )
    return frame


def service_score_anchor_year_months(source_cnxn, factor_ids) -> dict[int, int]:
    """Earliest حکم execute month that stores a user amount for each service factor."""
    ids = [int(x) for x in factor_ids]
    if not ids:
        return {}
    id_list = ",".join(str(i) for i in ids)
    df = pd.read_sql(
        f"""
        SELECT
            s.PAY_PfID_fk AS SourcePayrollFactorID,
            MIN(TRY_CAST(
                REPLACE(LEFT(LTRIM(RTRIM(rd.HRS_RdExcuteDate)), 7), N'/', N'')
                AS INT
            )) AS MinExecuteYM
        FROM dbo.HRS_RuleDocumentScores s
        INNER JOIN dbo.HRS_RuleDocument rd ON rd.HRS_RdID = s.HRS_RdID_fk
        WHERE s.PAY_PfID_fk IN ({id_list})
          AND ISNULL(TRY_CAST(s.HRS_RdsUserAmount AS float), 0) <> 0
        GROUP BY s.PAY_PfID_fk
        """,
        source_cnxn,
    )
    out = {}
    for _, row in df.iterrows():
        ym = normalize_shamsi_yyyymm(row["MinExecuteYM"])
        if ym is not None:
            out[int(row["SourcePayrollFactorID"])] = ym
    return out


def load_payment_system_factor_ets(source_cnxn) -> dict[int, set[int]]:
    """
    Factor → source ET IDs seen on احکام that use that factor,
    restricted to factors listed under a نظام پرداخت (PaymentSystemDetail).
    If a factor is not in any نظام, returns empty set (caller uses all ETs).
    """
    listed = pd.read_sql(
        """
        SELECT DISTINCT PAY_PfID_fk AS PfID
        FROM dbo.TBL_PaymentSystemDetail
        WHERE PAY_PfID_fk > 0 AND ISNULL(TBL_PsdActive, 1) = 1
        """,
        source_cnxn,
    )
    listed_ids = {int(x) for x in listed["PfID"].tolist()}
    if not listed_ids:
        return {}

    id_list = ",".join(str(x) for x in sorted(listed_ids))
    usage = pd.read_sql(
        f"""
        SELECT DISTINCT d.PAY_PfID_fk AS PfID, rd.TBL_EtID_fk AS EtID
        FROM dbo.HRS_RuleDocumentDetail d
        INNER JOIN dbo.HRS_RuleDocument rd ON rd.HRS_RdID = d.HRS_RdID_fk
        WHERE d.PAY_PfID_fk IN ({id_list})
          AND rd.TBL_EtID_fk IS NOT NULL AND rd.TBL_EtID_fk > 0
        """,
        source_cnxn,
    )
    out: dict[int, set[int]] = {pid: set() for pid in listed_ids}
    for _, r in usage.iterrows():
        out[int(r["PfID"])].add(int(r["EtID"]))
    return out


def _object_definition(source_cnxn, name: str) -> str:
    df = pd.read_sql(
        f"SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.{name}')) AS defn",
        source_cnxn,
    )
    if df.empty or df.iloc[0]["defn"] is None:
        return ""
    return str(df.iloc[0]["defn"])


def _date_to_yyyymm(d: str) -> int | None:
    return normalize_shamsi_yyyymm(d)


def _prev_yyyymm(ym: int) -> int:
    y, m = divmod(int(ym), 100)
    if m <= 1:
        return (y - 1) * 100 + 12
    return y * 100 + (m - 1)


def extract_cmsl_pf_block(udf_body: str, pf_id: int) -> str:
    """Return the SQL block for `if @PAY_PfID_fk={pf_id}` … until next live PfID if."""
    if not udf_body:
        return ""

    def _is_commented(match) -> bool:
        line_start = udf_body.rfind("\n", 0, match.start()) + 1
        return "--" in udf_body[line_start:match.start()]

    matches = [m for m in _CMSL_PF_IF_RE.finditer(udf_body) if not _is_commented(m)]
    candidates = []
    for i, m in enumerate(matches):
        if int(m.group(1)) != int(pf_id):
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(udf_body)
        candidates.append(udf_body[start:end].strip())

    if not candidates:
        return ""
    # Prefer the richest live block (most date branches / longest)
    candidates.sort(
        key=lambda b: (len(_DATE_CMP_RE.findall(b)), len(b)),
        reverse=True,
    )
    return candidates[0]


def extract_date_windows_from_block(block: str) -> list[dict]:
    """
    Build property date windows from HRS_RdExcuteDate comparisons in a UDF block.
    Each window: EffectiveFromMonth, EffectiveToMonth, SqlSnippet (= block leaf-ish).
    """
    if not block or not block.strip():
        return [
            {
                "EffectiveFromMonth": None,
                "EffectiveToMonth": None,
                "SqlSnippet": "",
                "notes": "empty_block",
            }
        ]

    dates = []
    for m in _DATE_CMP_RE.finditer(block):
        ym = _date_to_yyyymm(m.group(2))
        if ym:
            dates.append(ym)
    # also bare dates in quotes that look like branch points
    for m in _SHAMSI_DATE_RE.finditer(block):
        ym = _date_to_yyyymm(m.group(1))
        if ym and ym not in dates:
            # only keep if near a comparison (already collected) — skip extras
            pass

    uniq = sorted(set(dates))
    if not uniq:
        return [
            {
                "EffectiveFromMonth": None,
                "EffectiveToMonth": None,
                "SqlSnippet": block.strip(),
                "notes": "no_date_branch",
            }
        ]

    windows = []
    # open start → first date-1
    windows.append(
        {
            "EffectiveFromMonth": None,
            "EffectiveToMonth": _prev_yyyymm(uniq[0]),
            "SqlSnippet": block.strip(),
            "notes": f"before_{uniq[0]}",
        }
    )
    for i, ym in enumerate(uniq):
        to = _prev_yyyymm(uniq[i + 1]) if i + 1 < len(uniq) else None
        windows.append(
            {
                "EffectiveFromMonth": ym,
                "EffectiveToMonth": to,
                "SqlSnippet": block.strip(),
                "notes": f"from_{ym}",
            }
        )
    return windows


def _parse_employment_type_call(roll: str):
    calls = find_dispatcher_calls(roll or "")
    for c in calls:
        if c["variant"] == "PersonnelEmploymentType":
            return c
    # fallback: manual find if dbo. missing from KIND maps
    lower = (roll or "").lower()
    key = "fxpay_personnelemploymenttype"
    idx = lower.find(key)
    if idx < 0:
        return None
    # reuse find_dispatcher_calls — already covers it
    return None


def _dest_ets_for_factor(
    source_ets: list[int] | None,
    et_map: dict,
    payment_ets: set[int] | None,
    all_dest: list[int],
) -> list[int]:
    if source_ets is not None:
        out = []
        for sid in source_ets:
            if payment_ets is not None and payment_ets and int(sid) not in payment_ets:
                continue
            did = et_map.get(int(sid))
            if did is not None and did not in out:
                out.append(did)
        return out
    # no explicit source ET list → all mapped, optionally filtered by payment-system usage
    if payment_ets:
        out = []
        for sid in sorted(payment_ets):
            did = et_map.get(int(sid))
            if did is not None and did not in out:
                out.append(did)
        if out:
            return out
    return list(all_dest)


def build_roll_property_targets(source_cnxn, dest_cnxn, factor_ids: list[int] | None = None):
    """
    Returns (targets, stats, factor_df).
    Each target: SourcePayrollFactorID, EffectiveFromMonth, DestEmploymentTypeID,
    SlotCode, OutputExpr (roll arg or snippet), FormulaSql (roll or block), notes, ...
    """
    factors = load_parent2_factors(source_cnxn)
    if factor_ids is not None:
        want = {int(x) for x in factor_ids}
        factors = factors[factors["SourcePayrollFactorID"].isin(want)].copy()

    et_map = load_et_maps(dest_cnxn)
    all_dest = sorted(set(et_map.values()))
    pay_ets = load_payment_system_factor_ets(source_cnxn)
    factor_id_list = [int(x) for x in factors["SourcePayrollFactorID"].tolist()]
    anchors = factor_anchor_year_months(source_cnxn, factor_id_list)
    service_ids = [
        int(x)
        for x in factors.loc[factors["IsServiceHistory"].astype(int) == 1, "SourcePayrollFactorID"]
    ]
    for pf, ym in service_score_anchor_year_months(source_cnxn, service_ids).items():
        previous = anchors.get(pf)
        anchors[pf] = ym if previous is None else min(int(previous), int(ym))

    cmsl_body = _object_definition(source_cnxn, "FxHRS_RuleDocumentPersonnelCMSL")
    rd_personnel_body = _object_definition(source_cnxn, "FxHRS_RuleDocumentPersonnel")

    targets = []
    stats = defaultdict(int)

    for _, row in factors.iterrows():
        pf = int(row["SourcePayrollFactorID"])
        roll = (str(row["RollFormula"]).strip() if pd.notna(row["RollFormula"]) else "")
        name = str(row["FactorName"] or "")
        anchor = anchors.get(pf)
        payment_filter = pay_ets.get(pf)  # None if not in any PS listing
        is_service = int(row.get("IsServiceHistory") or 0) == 1

        stats["factors"] += 1
        if not roll or roll in ("0", "selef", "Self", "self"):
            # Still try CMSL if this PfID has a dedicated block there
            block = extract_cmsl_pf_block(cmsl_body, pf)
            if block and not is_service:
                roll = f"dbo.FxHRS_RuleDocumentPersonnelCMSL(HRS_RdID_fk,{pf})"
                stats["inferred_cmsl_roll"] += 1
            else:
                if is_service:
                    stats["service_history"] += 1
                    slot_label = "service_history_user_amount"
                    output_expr = "HRS_RdsUserAmount"
                    formula_sql = SERVICE_HISTORY_NOTE
                    dispatcher = "RuleDocumentPersonnelScoresCMSL"
                    notes = "service_history_user_amount"
                    trivial = False
                else:
                    stats["empty_roll"] += 1
                    slot_label = "no_roll_formula"
                    output_expr = roll or "0"
                    formula_sql = roll or "0"
                    dispatcher = None
                    notes = "empty_or_trivial_roll"
                    trivial = True
                dest_ets = _dest_ets_for_factor(None, et_map, payment_filter, all_dest)
                for det in dest_ets:
                    targets.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "FactorName": name,
                            "EffectiveFromMonth": anchor,
                            "EffectiveToMonth": None,
                            "DestEmploymentTypeID": int(det),
                            "SlotCode": "ALL",
                            "SlotLabel": slot_label,
                            "OutputExpr": output_expr,
                            "FormulaSql": formula_sql,
                            "dispatcher": dispatcher,
                            "notes": notes,
                            "IsTrivialZero": trivial,
                        }
                    )
                continue

        calls = _UDF_CALL_RE.findall(roll)
        primary = calls[0] if calls else None
        stats[f"udf_{primary or 'none'}"] += 1

        # --- EmploymentType dispatcher ---
        if primary and primary.lower() == "fxpay_personnelemploymenttype":
            et_call = None
            for c in find_dispatcher_calls(roll):
                if c["variant"] == "PersonnelEmploymentType":
                    et_call = c
                    break
            if not et_call or len(et_call["args"]) < 8:
                stats["et_parse_error"] += 1
                dest_ets = _dest_ets_for_factor(None, et_map, payment_filter, all_dest)
                for det in dest_ets:
                    targets.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "FactorName": name,
                            "EffectiveFromMonth": anchor,
                            "EffectiveToMonth": None,
                            "DestEmploymentTypeID": int(det),
                            "SlotCode": "ALL",
                            "SlotLabel": "et_parse_error",
                            "OutputExpr": roll,
                            "FormulaSql": roll,
                            "dispatcher": "PersonnelEmploymentType",
                            "notes": "could_not_parse_et_args",
                            "IsTrivialZero": is_trivial_zero(roll),
                        }
                    )
                continue

            # args: personnel, rd, F1..F6
            slots = {f"F{i}": et_call["args"][i + 1] for i in range(1, 7)}
            # Walk source ETs under EmploymentType rules
            for source_et in sorted(et_map.keys()):
                windows = et_slot_windows("PersonnelEmploymentType", source_et)
                if not windows:
                    continue
                if payment_filter is not None and payment_filter and source_et not in payment_filter:
                    continue
                dest_et = et_map.get(source_et)
                if dest_et is None:
                    continue
                for w_from, w_to, slot, w_note in windows:
                    if should_skip_slot(slot, "PersonnelEmploymentType"):
                        continue
                    expr = slots.get(slot) or "0"
                    if is_trivial_zero(expr):
                        stats["skipped_trivial_et_slot"] += 1
                        continue
                    # intersect with anchor: property starts at max(anchor, w_from)
                    prop_from = w_from
                    if anchor is not None:
                        if prop_from is None:
                            prop_from = anchor
                        else:
                            prop_from = max(int(prop_from), int(anchor))
                    if prop_from is None:
                        prop_from = anchor
                    notes = "roll:PersonnelEmploymentType"
                    if w_note:
                        notes += "; " + w_note
                    targets.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "FactorName": name,
                            "EffectiveFromMonth": prop_from,
                            "EffectiveToMonth": w_to,
                            "DestEmploymentTypeID": int(dest_et),
                            "SlotCode": slot,
                            "SlotLabel": (SLOT_META.get(slot) or {}).get("label"),
                            "OutputExpr": expr.strip(),
                            "FormulaSql": roll,
                            "dispatcher": "PersonnelEmploymentType",
                            "notes": notes,
                            "IsTrivialZero": False,
                        }
                    )
            continue

        # --- CMSL multi-factor UDF ---
        if primary and primary.lower() == "fxhrs_ruledocumentpersonnelcmsl":
            # expect (... , pfId)
            m = re.search(
                r"FxHRS_RuleDocumentPersonnelCMSL\s*\(\s*([^,]+)\s*,\s*(\d+)\s*\)",
                roll,
                re.I,
            )
            cmsl_pf = int(m.group(2)) if m else pf
            block = extract_cmsl_pf_block(cmsl_body, cmsl_pf)
            windows = extract_date_windows_from_block(block or roll)
            dest_ets = _dest_ets_for_factor(None, et_map, payment_filter, all_dest)
            for w in windows:
                prop_from = w["EffectiveFromMonth"]
                if prop_from is None:
                    prop_from = anchor
                elif anchor is not None:
                    # keep window start if later than factor birth
                    pass
                snippet = w.get("SqlSnippet") or block or roll
                # skip pure-zero early windows if snippet clearly only sets 0 and from is open
                for det in dest_ets:
                    targets.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "FactorName": name,
                            "EffectiveFromMonth": prop_from,
                            "EffectiveToMonth": w.get("EffectiveToMonth"),
                            "DestEmploymentTypeID": int(det),
                            "SlotCode": "CMSL",
                            "SlotLabel": "RuleDocumentPersonnelCMSL",
                            "OutputExpr": f"FxHRS_RuleDocumentPersonnelCMSL(HRS_RdID,{cmsl_pf}) /* {w.get('notes')} */",
                            "FormulaSql": snippet,
                            "dispatcher": "RuleDocumentPersonnelCMSL",
                            "notes": f"cmsl:{w.get('notes')}",
                            "IsTrivialZero": False,
                        }
                    )
            continue

        # --- RuleDocumentPersonnel(kind) or other UDFs ---
        if primary and primary.lower() == "fxhrs_ruledocumentpersonnel":
            block = rd_personnel_body
            # kind argument
            m = re.search(
                r"FxHRS_RuleDocumentPersonnel\s*\(\s*([^,]+)\s*,\s*(\d+)\s*\)",
                roll,
                re.I,
            )
            kind = int(m.group(2)) if m else None
            # date windows from whole UDF (shared) — coarse
            windows = extract_date_windows_from_block(block) if block else [
                {
                    "EffectiveFromMonth": anchor,
                    "EffectiveToMonth": None,
                    "SqlSnippet": roll,
                    "notes": "rd_personnel",
                }
            ]
            # Prefer fewer properties: unique from-months only at major dates,
            # but cap: if too many windows, collapse to single
            if len(windows) > 12:
                windows = [
                    {
                        "EffectiveFromMonth": anchor,
                        "EffectiveToMonth": None,
                        "SqlSnippet": roll,
                        "notes": "rd_personnel_collapsed",
                    }
                ]
            dest_ets = _dest_ets_for_factor(None, et_map, payment_filter, all_dest)
            for w in windows:
                prop_from = w["EffectiveFromMonth"] or anchor
                for det in dest_ets:
                    targets.append(
                        {
                            "SourcePayrollFactorID": pf,
                            "FactorName": name,
                            "EffectiveFromMonth": prop_from,
                            "EffectiveToMonth": w.get("EffectiveToMonth"),
                            "DestEmploymentTypeID": int(det),
                            "SlotCode": f"KIND{kind}" if kind is not None else "ALL",
                            "SlotLabel": "RuleDocumentPersonnel",
                            "OutputExpr": roll,
                            "FormulaSql": (w.get("SqlSnippet") or roll)[:8000],
                            "dispatcher": "RuleDocumentPersonnel",
                            "notes": f"rd_personnel_kind={kind};{w.get('notes')}",
                            "IsTrivialZero": is_trivial_zero(roll),
                        }
                    )
            continue

        # --- default: one property shell per ET ---
        dest_ets = _dest_ets_for_factor(None, et_map, payment_filter, all_dest)
        for det in dest_ets:
            targets.append(
                {
                    "SourcePayrollFactorID": pf,
                    "FactorName": name,
                    "EffectiveFromMonth": anchor,
                    "EffectiveToMonth": None,
                    "DestEmploymentTypeID": int(det),
                    "SlotCode": "ALL",
                    "SlotLabel": primary or "roll_raw",
                    "OutputExpr": roll,
                    "FormulaSql": roll,
                    "dispatcher": primary,
                    "notes": "roll_passthrough",
                    "IsTrivialZero": is_trivial_zero(roll),
                }
            )

    # Dedup (pf, from, dest_et) preferring non-trivial / more specific slot
    slot_rank = {"F1": 0, "F2": 1, "F3": 2, "F4": 3, "F5": 4, "CMSL": 5, "ALL": 9}
    targets.sort(
        key=lambda t: (
            t["SourcePayrollFactorID"],
            t["EffectiveFromMonth"] or 0,
            t["DestEmploymentTypeID"],
            1 if t.get("IsTrivialZero") else 0,
            slot_rank.get(t.get("SlotCode") or "ALL", 8),
        )
    )
    claimed = {}
    unique = []
    for t in targets:
        key = (
            t["SourcePayrollFactorID"],
            t["EffectiveFromMonth"],
            t["DestEmploymentTypeID"],
        )
        if key in claimed:
            stats["deduped"] += 1
            continue
        claimed[key] = t
        unique.append(t)

    stats["targets"] = len(unique)
    return unique, dict(stats), factors
