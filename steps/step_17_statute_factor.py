"""
Step 17: Migrate statute factors (PAY_PfParentID_fk = 2) → HCM3.StatuteFactor,
plus StatuteFactorProperty + Formula stubs.

Formulas come from PAY_PfRollFormula only. Property date × employment-type
windows are extracted from the scalar UDFs those rolls call
(FxPAY_PersonnelEmploymentType, FxHRS_RuleDocumentPersonnelCMSL, …).

FormulaBody stays `return 0;`; source SQL for the property is stored in Description.
"""
import re
import pandas as pd
import warnings
from pathlib import Path
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian
from utils.roll_formula_properties import (
    STATUTE_PARENT_ID,
    build_roll_property_targets,
    load_parent2_factors,
)

EXPERT_REVIEW_DIR = (
    Path(__file__).resolve().parent.parent
    / "analysis"
    / "statute_factor_parent2_roll"
    / "out"
)

warnings.filterwarnings('ignore', category=UserWarning)

# StatuteFactorPeriod / StatuteFactorType / StatuteFactorRelatedStatuteType
PERIOD_MONTHLY = 1
TYPE_PRIMARY = 1
TYPE_SECONDARY = 2
RELATED_IN_SERVICE = 1

PROPERTY_STATUS_ACTIVE = 1
FORMULA_MODULE_STAFF = 'Staff'
FORMULA_STUB_BODY = ' return 0;'
FORMULA_UIOBJECT_TEMPLATE = (
    Path(__file__).resolve().parent.parent / 'utils' / 'formula_uiobject_return0.bin'
)


def _load_formula_uiobject_template():
    if not FORMULA_UIOBJECT_TEMPLATE.exists():
        raise FileNotFoundError(
            f"Missing formula UIObject template: {FORMULA_UIOBJECT_TEMPLATE}. "
            "Export a Staff 'return 0;' UIObject blob from a working Rahkaran DB."
        )
    return FORMULA_UIOBJECT_TEMPLATE.read_bytes()


def setup_statute_factor_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'StatuteFactorMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.StatuteFactorMigrationMapping (
                SourcePayrollFactorID INT PRIMARY KEY,
                DestStatuteFactorID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_statute_factor_property_mapping_table(cursor, *, recreate_if_legacy=True):
    """
    Mapping keyed by (factor, EffectiveFromMonth YYYYMM, DestEmploymentTypeID).
    Legacy schemas are replaced when required columns are missing.
    """
    if recreate_if_legacy:
        cursor.execute("""
            IF EXISTS (
                SELECT * FROM master.sys.tables
                WHERE name = 'StatuteFactorPropertyMigrationMapping'
            )
            AND (
                COL_LENGTH(
                    'master.dbo.StatuteFactorPropertyMigrationMapping',
                    'EffectiveFromMonth'
                ) IS NULL
                OR COL_LENGTH(
                    'master.dbo.StatuteFactorPropertyMigrationMapping',
                    'SlotCode'
                ) IS NULL
                OR COL_LENGTH(
                    'master.dbo.StatuteFactorPropertyMigrationMapping',
                    'StatutePure'
                ) IS NULL
            )
            BEGIN
                DROP TABLE master.dbo.StatuteFactorPropertyMigrationMapping
            END
        """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables
            WHERE name = 'StatuteFactorPropertyMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.StatuteFactorPropertyMigrationMapping (
                SourcePayrollFactorID INT NOT NULL,
                EffectiveFromMonth INT NOT NULL,
                DestEmploymentTypeID BIGINT NOT NULL,
                SlotCode NVARCHAR(10) NULL,
                SourceBackFormulaID INT NULL,
                DestStatuteFactorPropertyID BIGINT NOT NULL,
                DestFormulaID BIGINT NOT NULL,
                OutputExpr NVARCHAR(MAX) NULL,
                StatutePure BIT NOT NULL DEFAULT 0,
                HasSelf BIT NOT NULL DEFAULT 0,
                NonRdFactorRefs NVARCHAR(200) NULL,
                DeferredReason NVARCHAR(1000) NULL,
                MigrationDate DATETIME DEFAULT GETDATE(),
                PRIMARY KEY (
                    SourcePayrollFactorID,
                    EffectiveFromMonth,
                    DestEmploymentTypeID
                )
            )
        END
    """)
    cursor.commit()


def _ensure_table_id(cursor, table_name, default_last_id=0):
    cursor.execute("""
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
    """, (table_name,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, ?)",
            (table_name, default_last_id),
        )
        return default_last_id
    return int(row[0])


def _unique_title(base_title, source_id, used_titles):
    title = (base_title or f'عامل {source_id}')[:400]
    if title not in used_titles:
        used_titles.add(title)
        return title
    suffix = f' ({source_id})'
    title = (title[: 400 - len(suffix)] + suffix)
    used_titles.add(title)
    return title


def _build_formula_description(target):
    source_pf_id = target["SourcePayrollFactorID"]
    month = target.get("EffectiveFromMonth")
    slot = target.get("SlotCode")
    expr = target.get("OutputExpr") or ""
    source_kind = target.get("SourceKind") or "formula"
    raw = target.get("FormulaSql") or ""
    pure = bool(target.get("StatutePure"))
    deferred = target.get("DeferredReason") or ""
    header = (
        f"[Migrated from source {source_kind}]\n"
        f"SourcePayrollFactorID={source_pf_id}; "
        f"EffectiveFromMonth={month}; Slot={slot}; "
        f"Dispatcher={target.get('dispatcher')}\n"
        f"StatutePure={'yes' if pure else 'no'}; "
        f"HasSelf={'yes' if target.get('HasSelf') else 'no'}\n"
        f"RdFactorRefs={target.get('RdFactorRefs') or ''}\n"
        f"NonRdFactorRefs={target.get('NonRdFactorRefs') or ''}\n"
        f"DeferredReason={deferred or '(none)'}\n"
        f"OutputExpr={expr}\n"
        f"NOTE: Statute factors are evaluated at حکم issue (not monthly payroll). "
        f"Non-RD refs (attendance/other) are deferred — FormulaBody is a stub.\n"
        f"---\n"
    )
    body = raw
    max_sql = 80000
    if len(body) > max_sql:
        body = body[:max_sql] + "\n...[truncated]"
    return header + body


def _clear_previously_migrated_properties(dest_cursor, dest_cnxn):
    """
    Remove prior property/formula migration so we can rebuild from formula breaks.
    """
    has_prop_map = pd.read_sql("""
        SELECT CASE WHEN OBJECT_ID('master.dbo.StatuteFactorPropertyMigrationMapping')
                    IS NOT NULL THEN 1 ELSE 0 END AS HasMap
    """, dest_cnxn).iloc[0]['HasMap'] == 1

    if has_prop_map:
        dest_cursor.execute("""
            IF COL_LENGTH(
                'master.dbo.StatuteFactorPropertyMigrationMapping',
                'DestStatuteFactorPropertyID'
            ) IS NOT NULL
            BEGIN
                DELETE sfp
                FROM HCM3.StatuteFactorProperty sfp
                INNER JOIN master.dbo.StatuteFactorPropertyMigrationMapping m
                    ON sfp.StatuteFactorPropertyID = m.DestStatuteFactorPropertyID
            END
        """)
        dest_cursor.execute("""
            IF COL_LENGTH('master.dbo.StatuteFactorPropertyMigrationMapping', 'DestFormulaID') IS NOT NULL
            BEGIN
                DELETE f
                FROM HCM3.Formula f
                INNER JOIN master.dbo.StatuteFactorPropertyMigrationMapping m
                    ON f.FormulaID = m.DestFormulaID
                WHERE NOT EXISTS (
                    SELECT 1 FROM HCM3.StatuteFactorProperty sfp
                    WHERE sfp.FormulaRef = f.FormulaID
                )
            END
        """)
        dest_cursor.execute(
            "DELETE FROM master.dbo.StatuteFactorPropertyMigrationMapping"
        )

    dest_cursor.execute("""
        DELETE sfp
        FROM HCM3.StatuteFactorProperty sfp
        INNER JOIN master.dbo.StatuteFactorMigrationMapping m
            ON sfp.StatuteFactorRef = m.DestStatuteFactorID
    """)
    dest_cursor.execute("""
        DELETE f
        FROM HCM3.Formula f
        WHERE f.ModuleName = 'Staff'
          AND f.Description LIKE N'[Migrated from source%'
          AND NOT EXISTS (
              SELECT 1 FROM HCM3.StatuteFactorProperty sfp
              WHERE sfp.FormulaRef = f.FormulaID
          )
    """)
    print("  -> Cleared previous migrated statute-factor properties/formulas.")


def _purge_non_parent2_statute_factors(source_cnxn, dest_cnxn, dest_cursor):
    """Remove previously mapped StatuteFactors that are not parent=2."""
    keep_ids = set(
        load_parent2_factors(source_cnxn)["SourcePayrollFactorID"].astype(int).tolist()
    )
    mapped_df = pd.read_sql(
        """
        SELECT SourcePayrollFactorID, DestStatuteFactorID
        FROM master.dbo.StatuteFactorMigrationMapping
        """,
        dest_cnxn,
    )
    if mapped_df.empty:
        return 0

    drop = mapped_df[
        ~mapped_df["SourcePayrollFactorID"].astype(int).isin(keep_ids)
    ]
    if drop.empty:
        print("  -> No non-parent=2 statute factors to purge.")
        return 0

    dest_ids = [int(x) for x in drop["DestStatuteFactorID"].tolist()]
    src_ids = [int(x) for x in drop["SourcePayrollFactorID"].tolist()]
    dest_list = ",".join(str(x) for x in dest_ids)
    src_list = ",".join(str(x) for x in src_ids)
    print(
        f"  -> Purging {len(dest_ids)} non-parent={STATUTE_PARENT_ID} "
        f"statute factors from destination..."
    )

    dest_cursor.execute(f"""
        DELETE FROM HCM3.EmployeeStatuteFactor
        WHERE StatuteFactorPropertyRef IN (
            SELECT StatuteFactorPropertyID FROM HCM3.StatuteFactorProperty
            WHERE StatuteFactorRef IN ({dest_list})
        )
    """)
    dest_cursor.execute(f"""
        DELETE FROM HCM3.StatuteFactorProperty
        WHERE StatuteFactorRef IN ({dest_list})
    """)
    dest_cursor.execute(f"""
        DELETE FROM HCM3.StatuteFactor
        WHERE StatuteFactorID IN ({dest_list})
    """)
    dest_cursor.execute(f"""
        DELETE FROM master.dbo.StatuteFactorMigrationMapping
        WHERE SourcePayrollFactorID IN ({src_list})
    """)
    return len(dest_ids)


def _migrate_factor_masters(source_cnxn, dest_cnxn, dest_cursor):
    print(
        f"  -> Loading PAY_PayrollFactor WHERE PAY_PfParentID_fk = "
        f"{STATUTE_PARENT_ID}..."
    )
    factors_df = load_parent2_factors(source_cnxn)
    print(f"  -> Found {len(factors_df)} parent={STATUTE_PARENT_ID} factors.")

    mapping_df = pd.read_sql(
        "SELECT SourcePayrollFactorID, DestStatuteFactorID "
        "FROM master.dbo.StatuteFactorMigrationMapping",
        dest_cnxn,
    )
    existing_map = {
        int(r["SourcePayrollFactorID"]): int(r["DestStatuteFactorID"])
        for _, r in mapping_df.iterrows()
    }

    existing_titles = set(
        pd.read_sql(
            "SELECT Title FROM HCM3.StatuteFactor", dest_cnxn
        )["Title"].dropna().tolist()
    )
    used_titles = {normalize_persian(t) for t in existing_titles}

    last_id = _ensure_table_id(dest_cursor, "HCM3.StatuteFactor")
    next_id = last_id + 1
    inserted = 0
    linked = 0

    for _, row in factors_df.iterrows():
        src_id = int(row["SourcePayrollFactorID"])
        if src_id in existing_map:
            linked += 1
            continue

        title_src = row.get("PAY_PfTitle") or row.get("FactorName")
        title = _unique_title(
            clean_persian_text(title_src), src_id, used_titles
        )
        factor_type = (
            TYPE_PRIMARY
            if int(row.get("PAY_PfInsuranceStatus") or 0) == 1
            else TYPE_SECONDARY
        )
        note = row.get("FactorNote")
        description = (
            clean_persian_text(note)
            if note is not None and str(note).strip()
            else f"Migrated from PAY_PayrollFactor {src_id} (parent={STATUTE_PARENT_ID})"
        )
        name = f"Mig_Pf_{src_id}"
        dest_cursor.execute("""
            INSERT INTO HCM3.StatuteFactor (
                StatuteFactorID, Name, Title, PeriodCode, TypeCode,
                RelatedStatuteTypeCode, Description,
                VisibleInStatute, VisibleInEmployee,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, GETDATE(), 1, GETDATE(), 1)
        """, (
            next_id,
            name,
            title,
            PERIOD_MONTHLY,
            factor_type,
            RELATED_IN_SERVICE,
            description,
        ))
        dest_cursor.execute("""
            INSERT INTO master.dbo.StatuteFactorMigrationMapping
                (SourcePayrollFactorID, DestStatuteFactorID)
            VALUES (?, ?)
        """, (src_id, next_id))
        next_id += 1
        inserted += 1

    if inserted:
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (next_id - 1, "HCM3.StatuteFactor"),
        )

    print(
        f"  -> StatuteFactor masters: inserted={inserted}, "
        f"already_mapped={linked}, catalog={len(factors_df)}."
    )


def _annotate_roll_target(t: dict) -> dict:
    """Lightweight purity flags for Roll/UDF-backed property shells."""
    sql = (t.get("FormulaSql") or "") + "\n" + (t.get("OutputExpr") or "")
    has_self = bool(re.search(r"\bSelf\b", sql, re.I))
    has_udf = bool(t.get("dispatcher")) or bool(
        re.search(r"\bFx[A-Za-z0-9_]+\s*\(", sql, re.I)
    )
    # Roll-era formulas always need expert body; mark deferred when UDF-backed.
    pure = False
    deferred = None
    if t.get("IsTrivialZero"):
        deferred = "trivial_zero_roll"
    elif has_udf:
        deferred = f"udf_roll:{t.get('dispatcher') or 'Fx*'}"
    else:
        deferred = "roll_needs_expert_formula"
    t["StatutePure"] = pure
    t["HasSelf"] = has_self
    t["DeferredReason"] = deferred
    t["SourceKind"] = f"RollFormula/{t.get('dispatcher') or 'raw'}"
    t["RdFactorRefs"] = ""
    t["NonRdFactorRefs"] = ""
    return t


def _migrate_factor_properties(source_cnxn, dest_cnxn, dest_cursor):
    print(
        "  -> Building RollFormula property targets "
        "(UDF date/ET windows)..."
    )
    targets, stats, _factors = build_roll_property_targets(source_cnxn, dest_cnxn)
    print(f"  -> Roll property stats: {stats}")
    print(f"  -> Built {len(targets)} property targets.")

    setup_statute_factor_property_mapping_table(
        dest_cursor, recreate_if_legacy=True
    )
    _clear_previously_migrated_properties(dest_cursor, dest_cnxn)

    factor_map = {
        int(r["SourcePayrollFactorID"]): int(r["DestStatuteFactorID"])
        for _, r in pd.read_sql(
            "SELECT SourcePayrollFactorID, DestStatuteFactorID "
            "FROM master.dbo.StatuteFactorMigrationMapping",
            dest_cnxn,
        ).iterrows()
    }
    if not factor_map:
        print("  -> No mapped statute factors; skip properties.")
        return

    uiobject = _load_formula_uiobject_template()
    formula_last_id = _ensure_table_id(dest_cursor, "HCM3.Formula", 0)
    property_last_id = _ensure_table_id(dest_cursor, "HCM3.StatuteFactorProperty", 0)

    insert_formula_sql = """
        INSERT INTO HCM3.Formula (
            FormulaID, FormulaBody, UIObject, ModuleName, Description,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_property_sql = """
        INSERT INTO HCM3.StatuteFactorProperty (
            StatuteFactorPropertyID, StatuteFactorRef, EmploymentTypeRef,
            IssueYearMonth, ApplyYearMonth, Status, FormulaRef,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_prop_map_sql = """
        INSERT INTO master.dbo.StatuteFactorPropertyMigrationMapping (
            SourcePayrollFactorID, EffectiveFromMonth, DestEmploymentTypeID,
            SlotCode, SourceBackFormulaID, DestStatuteFactorPropertyID,
            DestFormulaID, OutputExpr, StatutePure, HasSelf,
            NonRdFactorRefs, DeferredReason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    formula_cache = {}
    formulas_inserted = 0
    properties_inserted = 0
    properties_pure = 0
    properties_deferred = 0
    skipped_no_factor = 0
    skipped_no_month = 0

    for raw in targets:
        t = _annotate_roll_target(dict(raw))
        src_pf = int(t["SourcePayrollFactorID"])
        dest_factor_id = factor_map.get(src_pf)
        if not dest_factor_id:
            skipped_no_factor += 1
            continue

        frm = t.get("EffectiveFromMonth")
        if frm is None or (isinstance(frm, float) and pd.isna(frm)):
            skipped_no_month += 1
            continue
        from_month = int(frm)
        et_id = int(t["DestEmploymentTypeID"])
        slot = t.get("SlotCode") or "ALL"
        expr = t.get("OutputExpr") or ""
        pure = 1 if t.get("StatutePure") else 0
        has_self = 1 if t.get("HasSelf") else 0
        deferred = t.get("DeferredReason")

        formula_key = (src_pf, from_month, slot, expr, pure)
        formula_id = formula_cache.get(formula_key)
        if formula_id is None:
            formula_last_id += 1
            dest_cursor.execute(
                insert_formula_sql,
                (
                    formula_last_id,
                    FORMULA_STUB_BODY,
                    uiobject,
                    FORMULA_MODULE_STAFF,
                    _build_formula_description(t),
                ),
            )
            formula_id = formula_last_id
            formula_cache[formula_key] = formula_id
            formulas_inserted += 1

        property_last_id += 1
        dest_cursor.execute(
            insert_property_sql,
            (
                property_last_id,
                dest_factor_id,
                et_id,
                from_month,
                from_month,
                PROPERTY_STATUS_ACTIVE,
                formula_id,
            ),
        )
        dest_cursor.execute(
            insert_prop_map_sql,
            (
                src_pf,
                from_month,
                et_id,
                slot,
                None,
                property_last_id,
                formula_id,
                expr[:4000] if expr else None,
                pure,
                has_self,
                None,
                (deferred[:1000] if deferred else None),
            ),
        )
        properties_inserted += 1
        if pure:
            properties_pure += 1
        else:
            properties_deferred += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
        (formula_last_id, "HCM3.Formula"),
    )
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
        (property_last_id, "HCM3.StatuteFactorProperty"),
    )

    print(
        f"  -> Formulas inserted: {formulas_inserted}. "
        f"Properties inserted: {properties_inserted} "
        f"(statute-pure={properties_pure}, deferred={properties_deferred}). "
        f"Skipped no-factor={skipped_no_factor}, no-month={skipped_no_month}."
    )


def _write_expert_factor_review(source_cnxn, dest_cnxn):
    EXPERT_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    factors = load_parent2_factors(source_cnxn)
    mapping = pd.read_sql(
        "SELECT SourcePayrollFactorID, DestStatuteFactorID "
        "FROM master.dbo.StatuteFactorMigrationMapping",
        dest_cnxn,
    )
    props = pd.read_sql(
        """
        SELECT SourcePayrollFactorID, EffectiveFromMonth, DestEmploymentTypeID,
               SlotCode, OutputExpr, StatutePure, HasSelf, DeferredReason
        FROM master.dbo.StatuteFactorPropertyMigrationMapping
        """,
        dest_cnxn,
    )

    factors = factors.merge(
        mapping,
        on="SourcePayrollFactorID",
        how="left",
    )
    prop_counts = (
        props.groupby("SourcePayrollFactorID")
        .agg(
            PropertyCount=("DestEmploymentTypeID", "size"),
            DistinctMonths=("EffectiveFromMonth", "nunique"),
            DistinctSlots=("SlotCode", "nunique"),
        )
        .reset_index()
    )
    factors = factors.merge(prop_counts, on="SourcePayrollFactorID", how="left")
    for col in ("PropertyCount", "DistinctMonths", "DistinctSlots"):
        factors[col] = factors[col].fillna(0).astype(int)

    out_csv = EXPERT_REVIEW_DIR / "parent2_factors.csv"
    factors.to_csv(out_csv, index=False, encoding="utf-8-sig")

    # Sample interesting factors
    sample_ids = [134, 175, 186, 112]
    sample = props[props["SourcePayrollFactorID"].isin(sample_ids)].copy()
    sample.to_csv(
        EXPERT_REVIEW_DIR / "sample_134_175_186_112.csv",
        index=False,
        encoding="utf-8-sig",
    )

    md_lines = [
        "# Parent=2 statute factors (RollFormula → properties)",
        "",
        f"- Catalog size: **{len(factors)}**",
        f"- Properties mapped: **{len(props)}**",
        f"- Factors with ≥1 property: **{(factors['PropertyCount'] > 0).sum()}**",
        "",
        "## Sample property rows (134 / 175 / 186 / 112)",
        "",
    ]
    if not sample.empty:
        g = (
            sample.groupby(
                ["SourcePayrollFactorID", "SlotCode", "EffectiveFromMonth"]
            )
            .size()
            .reset_index(name="rows")
        )
        for _, r in g.iterrows():
            md_lines.append(
                f"- pf={int(r['SourcePayrollFactorID'])} "
                f"slot={r['SlotCode']} from={int(r['EffectiveFromMonth'])} "
                f"rows={int(r['rows'])}"
            )
    else:
        md_lines.append("_no sample rows_")
    md_lines.extend(["", "See `parent2_factors.csv`.", ""])
    (EXPERT_REVIEW_DIR / "README.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )
    print(f"  -> Expert review written under {EXPERT_REVIEW_DIR}")


def run():
    print(
        f"\n--- Running Step 17: Statute Factor Migration "
        f"(parent={STATUTE_PARENT_ID} + RollFormula) ---"
    )

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_statute_factor_mapping_table(dest_cursor)
        setup_statute_factor_property_mapping_table(
            dest_cursor, recreate_if_legacy=False
        )

        _purge_non_parent2_statute_factors(source_cnxn, dest_cnxn, dest_cursor)
        _migrate_factor_masters(source_cnxn, dest_cnxn, dest_cursor)
        _migrate_factor_properties(source_cnxn, dest_cnxn, dest_cursor)
        _write_expert_factor_review(source_cnxn, dest_cnxn)

        dest_cnxn.commit()
        print(
            "Success! Parent=2 statute factors + RollFormula properties "
            "migrated. See analysis/statute_factor_parent2_roll/out."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Statute Factor step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        dest_cnxn.close()
        source_cnxn.close()
