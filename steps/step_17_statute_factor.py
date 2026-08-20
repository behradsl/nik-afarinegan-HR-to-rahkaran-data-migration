"""
Step 17: Migrate PAY_PayrollFactor (used on rule docs) → HCM3.StatuteFactor,
plus StatuteFactorProperty + Formula from PAY_PayrollBackFormula / PAY_PfFormula.

Source has no separate formula-per-employment-type rows (ET branching is inside
SQL helpers like FxPAY_PersonnelKind). Rahkaran requires non-null EmploymentTypeRef
on StatuteFactorProperty, so each dated formula is applied to all destination
employment types (one property row per ET, shared FormulaRef).

IssueYearMonth / ApplyYearMonth come from PAY_MonthID (Shamsi YYYYMM).

Source formulas are SQL; Rahkaran expects C#. We store a safe stub body
(`return 0;`) plus a designer UIObject template so the formula editor can open,
and keep the original SQL in Formula.Description for rebuild.
"""
import pandas as pd
import warnings
from pathlib import Path
from db_core import get_connections
from utils.data_helpers import clean_persian_text

warnings.filterwarnings('ignore', category=UserWarning)

# StatuteFactorPeriod / StatuteFactorType / StatuteFactorRelatedStatuteType
PERIOD_MONTHLY = 1
TYPE_PRIMARY = 1
TYPE_SECONDARY = 2
RELATED_IN_SERVICE = 1

PROPERTY_STATUS_ACTIVE = 1
FORMULA_MODULE_STAFF = 'Staff'
# Match Rahkaran designer stub used by utils/formula_uiobject_return0.bin
FORMULA_STUB_BODY = ' return 0;'
FORMULA_UIOBJECT_TEMPLATE = Path(__file__).resolve().parent.parent / (
    'utils' / 'formula_uiobject_return0.bin'
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
    Mapping includes DestEmploymentTypeID. Older schema (factor+month only) is
    replaced so we can expand one source formula version across all ETs.
    """
    if recreate_if_legacy:
        cursor.execute("""
            IF EXISTS (
                SELECT * FROM master.sys.tables
                WHERE name = 'StatuteFactorPropertyMigrationMapping'
            )
            AND NOT EXISTS (
                SELECT * FROM master.sys.columns
                WHERE object_id = OBJECT_ID('master.dbo.StatuteFactorPropertyMigrationMapping')
                  AND name = 'DestEmploymentTypeID'
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
                SourceMonthID INT NOT NULL,
                DestEmploymentTypeID BIGINT NOT NULL,
                SourceBackFormulaID INT NULL,
                DestStatuteFactorPropertyID BIGINT NOT NULL,
                DestFormulaID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE(),
                PRIMARY KEY (SourcePayrollFactorID, SourceMonthID, DestEmploymentTypeID)
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


def _formula_text(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text


def _build_formula_description(source_pf_id, month_id, source_sql, source_kind):
    header = (
        f"[Migrated from source {source_kind}]\n"
        f"SourcePayrollFactorID={source_pf_id}; MonthID={month_id}\n"
        f"NOTE: Original engine is SQL. Rahkaran FormulaBody is a stub; rewrite in app.\n"
        f"---\n"
    )
    body = source_sql or ''
    max_sql = 80000
    if len(body) > max_sql:
        body = body[:max_sql] + '\n...[truncated]'
    return header + body


def _dest_employment_type_ids(dest_cnxn):
    """Prefer mapped ETs; fall back to all HCM3.EmploymentType rows."""
    mapped = pd.read_sql("""
        SELECT DestEmploymentTypeID
        FROM master.dbo.EmploymentTypeMigrationMapping
        ORDER BY DestEmploymentTypeID
    """, dest_cnxn)
    if not mapped.empty:
        return [int(x) for x in mapped['DestEmploymentTypeID'].tolist()]
    all_et = pd.read_sql("""
        SELECT EmploymentTypeID
        FROM HCM3.EmploymentType
        ORDER BY EmploymentTypeID
    """, dest_cnxn)
    return [int(x) for x in all_et['EmploymentTypeID'].tolist()]


def _clear_previously_migrated_properties(dest_cursor, dest_cnxn):
    """
    Remove prior property/formula migration so we can rebuild with ET expansion.
    Also clears any null-ET properties hanging off migrated statute factors.
    """
    has_prop_map = pd.read_sql("""
        SELECT CASE WHEN OBJECT_ID('master.dbo.StatuteFactorPropertyMigrationMapping')
                    IS NOT NULL THEN 1 ELSE 0 END AS HasMap
    """, dest_cnxn).iloc[0]['HasMap'] == 1

    if has_prop_map:
        # Delete properties before formulas (FK FormulaRef)
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

    # Any remaining properties on migrated statute factors
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


def _migrate_factor_masters(source_cnxn, dest_cnxn, dest_cursor):
    print("Fetching payroll factors used on rule documents...")
    source_df = pd.read_sql("""
        SELECT
            pf.PAY_PfID AS SourcePayrollFactorID,
            pf.PAY_PfName AS FactorName,
            pf.PAY_PfNote AS FactorNote,
            pf.PAY_PfFieldName AS FieldName,
            CASE WHEN d.PAY_PfID_fk IS NOT NULL THEN 1 ELSE 0 END AS InDetail,
            CASE WHEN s.PAY_PfID_fk IS NOT NULL THEN 1 ELSE 0 END AS InScore
        FROM dbo.PAY_PayrollFactor pf
        LEFT JOIN (
            SELECT DISTINCT PAY_PfID_fk
            FROM dbo.HRS_RuleDocumentDetail
            WHERE PAY_PfID_fk > 0
        ) d ON d.PAY_PfID_fk = pf.PAY_PfID
        LEFT JOIN (
            SELECT DISTINCT PAY_PfID_fk
            FROM dbo.HRS_RuleDocumentScores
            WHERE PAY_PfID_fk > 0
        ) s ON s.PAY_PfID_fk = pf.PAY_PfID
        WHERE pf.PAY_PfID > 0
          AND (d.PAY_PfID_fk IS NOT NULL OR s.PAY_PfID_fk IS NOT NULL)
        ORDER BY pf.PAY_PfID
    """, source_cnxn)

    if source_df.empty:
        print("No statute-related payroll factors found.")
        return

    source_df['FactorName'] = source_df['FactorName'].apply(clean_persian_text)
    source_df['FactorNote'] = source_df['FactorNote'].apply(
        lambda x: clean_persian_text(x) if pd.notna(x) else None
    )

    mapped_df = pd.read_sql(
        "SELECT SourcePayrollFactorID, DestStatuteFactorID "
        "FROM master.dbo.StatuteFactorMigrationMapping",
        dest_cnxn,
    )
    already = set(int(x) for x in mapped_df['SourcePayrollFactorID'].tolist())
    pending = source_df[~source_df['SourcePayrollFactorID'].isin(already)].copy()

    print(
        f"  -> Candidates: {len(source_df)}. "
        f"Already mapped: {len(already)}. To insert: {len(pending)}."
    )
    if pending.empty:
        print("  -> All statute factor masters already mapped.")
        return

    existing_titles = set(
        pd.read_sql("SELECT Title FROM HCM3.StatuteFactor", dest_cnxn)['Title']
        .dropna()
        .tolist()
    )
    used_titles = {str(t) for t in existing_titles}

    last_id = _ensure_table_id(dest_cursor, 'HCM3.StatuteFactor', 0)
    insert_sql = """
        INSERT INTO HCM3.StatuteFactor (
            StatuteFactorID, Name, Title, PeriodCode, TypeCode,
            RelatedStatuteTypeCode, Description,
            VisibleInStatute, VisibleInEmployee,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, GETDATE(), 1, GETDATE(), 1)
    """
    map_sql = """
        INSERT INTO master.dbo.StatuteFactorMigrationMapping (
            SourcePayrollFactorID, DestStatuteFactorID
        ) VALUES (?, ?)
    """

    inserted = 0
    for _, row in pending.iterrows():
        source_id = int(row['SourcePayrollFactorID'])
        title = _unique_title(row['FactorName'], source_id, used_titles)
        name = f'Mig_Pf_{source_id}'
        type_code = TYPE_PRIMARY if int(row['InDetail']) == 1 else TYPE_SECONDARY
        note = row['FactorNote']
        if note and len(note) > 0:
            description = note
        else:
            field = row['FieldName']
            description = (
                str(field).strip()
                if pd.notna(field) and str(field).strip() not in ('', '0')
                else None
            )

        last_id += 1
        dest_cursor.execute(
            insert_sql,
            (
                last_id,
                name,
                title,
                PERIOD_MONTHLY,
                type_code,
                RELATED_IN_SERVICE,
                description,
            ),
        )
        dest_cursor.execute(map_sql, (source_id, last_id))
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
        (last_id, 'HCM3.StatuteFactor'),
    )
    print(
        f"  -> StatuteFactors inserted: {inserted}. "
        f"Skipped (already mapped): {len(already)}."
    )


def _collect_source_formula_versions(source_cnxn, mapped_factor_ids):
    """
    Build one formula version per (SourcePayrollFactorID, MonthID).
    Prefer PAY_PayrollBackFormula; if a factor has none, fall back to PAY_PfFormula
    with the latest PAY_MonthID.
    """
    if not mapped_factor_ids:
        return pd.DataFrame()

    id_list = ",".join(str(int(x)) for x in sorted(mapped_factor_ids))

    back_df = pd.read_sql(f"""
        SELECT
            b.PAY_PbfID AS SourceBackFormulaID,
            b.PAY_PfID_fk AS SourcePayrollFactorID,
            b.PAY_MonthID_fk AS SourceMonthID,
            b.PAY_PbfFormula AS FormulaSql
        FROM dbo.PAY_PayrollBackFormula b
        WHERE ISNULL(b.PAY_PbfActive, 1) = 1
          AND b.PAY_PfID_fk IN ({id_list})
          AND b.PAY_MonthID_fk IS NOT NULL
          AND b.PAY_MonthID_fk > 0
          AND b.PAY_PbfFormula IS NOT NULL
          AND LTRIM(RTRIM(b.PAY_PbfFormula)) <> ''
    """, source_cnxn)

    if not back_df.empty:
        back_df = back_df.sort_values(
            ['SourcePayrollFactorID', 'SourceMonthID', 'SourceBackFormulaID']
        )
        back_df = back_df.drop_duplicates(
            subset=['SourcePayrollFactorID', 'SourceMonthID'],
            keep='last',
        ).copy()
        back_df['SourceKind'] = 'PAY_PayrollBackFormula'
    else:
        back_df = pd.DataFrame(
            columns=[
                'SourceBackFormulaID', 'SourcePayrollFactorID',
                'SourceMonthID', 'FormulaSql', 'SourceKind',
            ]
        )

    factors_with_back = set(
        int(x) for x in back_df['SourcePayrollFactorID'].tolist()
    ) if not back_df.empty else set()
    missing = [i for i in mapped_factor_ids if int(i) not in factors_with_back]

    fallback_rows = []
    if missing:
        miss_list = ",".join(str(int(x)) for x in missing)
        latest_month = pd.read_sql("""
            SELECT MAX(Pay_MonthID) AS MaxMonth
            FROM dbo.PAY_Month
            WHERE Pay_MonthID > 0
        """, source_cnxn).iloc[0]['MaxMonth']
        if latest_month is None or (isinstance(latest_month, float) and pd.isna(latest_month)):
            latest_month = 140401
        else:
            latest_month = int(latest_month)

        pf_df = pd.read_sql(f"""
            SELECT
                PAY_PfID AS SourcePayrollFactorID,
                PAY_PfFormula AS FormulaSql
            FROM dbo.PAY_PayrollFactor
            WHERE PAY_PfID IN ({miss_list})
              AND PAY_PfFormula IS NOT NULL
              AND LTRIM(RTRIM(PAY_PfFormula)) <> ''
        """, source_cnxn)
        for _, row in pf_df.iterrows():
            sql = _formula_text(row['FormulaSql'])
            if sql is None:
                continue
            fallback_rows.append({
                'SourceBackFormulaID': None,
                'SourcePayrollFactorID': int(row['SourcePayrollFactorID']),
                'SourceMonthID': latest_month,
                'FormulaSql': sql,
                'SourceKind': 'PAY_PayrollFactor.PAY_PfFormula',
            })

    if fallback_rows:
        back_df = pd.concat([back_df, pd.DataFrame(fallback_rows)], ignore_index=True)

    if back_df.empty:
        return back_df

    back_df['FormulaSql'] = back_df['FormulaSql'].apply(_formula_text)
    back_df = back_df[back_df['FormulaSql'].notna()].copy()
    return back_df


def _migrate_factor_properties(source_cnxn, dest_cnxn, dest_cursor):
    print("Migrating StatuteFactorProperty + Formula (expand to all employment types)...")

    factor_map_df = pd.read_sql("""
        SELECT SourcePayrollFactorID, DestStatuteFactorID
        FROM master.dbo.StatuteFactorMigrationMapping
    """, dest_cnxn)
    if factor_map_df.empty:
        print("  -> No mapped statute factors; skip properties.")
        return

    et_ids = _dest_employment_type_ids(dest_cnxn)
    if not et_ids:
        print("  -> No destination employment types found; skip properties.")
        return
    print(f"  -> Employment types to apply: {len(et_ids)}.")

    _clear_previously_migrated_properties(dest_cursor, dest_cnxn)
    # Recreate mapping table if legacy schema was dropped during clear/setup
    setup_statute_factor_property_mapping_table(dest_cursor, recreate_if_legacy=True)

    factor_map = {
        int(r['SourcePayrollFactorID']): int(r['DestStatuteFactorID'])
        for _, r in factor_map_df.iterrows()
    }

    versions_df = _collect_source_formula_versions(source_cnxn, list(factor_map.keys()))
    if versions_df.empty:
        print("  -> No source formula versions found for mapped factors.")
        return

    formula_last_id = _ensure_table_id(dest_cursor, 'HCM3.Formula', 0)
    property_last_id = _ensure_table_id(dest_cursor, 'HCM3.StatuteFactorProperty', 0)
    uiobject_blob = _load_formula_uiobject_template()

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
            SourcePayrollFactorID, SourceMonthID, DestEmploymentTypeID,
            SourceBackFormulaID, DestStatuteFactorPropertyID, DestFormulaID
        ) VALUES (?, ?, ?, ?, ?, ?)
    """

    formulas_inserted = 0
    properties_inserted = 0
    skipped_no_factor = 0

    for _, row in versions_df.iterrows():
        source_pf_id = int(row['SourcePayrollFactorID'])
        month_id = int(row['SourceMonthID'])
        dest_factor_id = factor_map.get(source_pf_id)
        if dest_factor_id is None:
            skipped_no_factor += 1
            continue

        source_sql = row['FormulaSql']
        description = _build_formula_description(
            source_pf_id, month_id, source_sql, row.get('SourceKind') or 'formula'
        )
        back_id = row.get('SourceBackFormulaID')
        if back_id is not None and not (isinstance(back_id, float) and pd.isna(back_id)):
            back_id = int(back_id)
        else:
            back_id = None

        # One shared formula for all employment types of this version
        formula_last_id += 1
        dest_cursor.execute(
            insert_formula_sql,
            (
                formula_last_id,
                FORMULA_STUB_BODY,
                uiobject_blob,
                FORMULA_MODULE_STAFF,
                description,
            ),
        )
        formulas_inserted += 1

        for et_id in et_ids:
            property_last_id += 1
            dest_cursor.execute(
                insert_property_sql,
                (
                    property_last_id,
                    dest_factor_id,
                    et_id,
                    month_id,
                    month_id,
                    PROPERTY_STATUS_ACTIVE,
                    formula_last_id,
                ),
            )
            dest_cursor.execute(
                insert_prop_map_sql,
                (
                    source_pf_id,
                    month_id,
                    et_id,
                    back_id,
                    property_last_id,
                    formula_last_id,
                ),
            )
            properties_inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
        (formula_last_id, 'HCM3.Formula'),
    )
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
        (property_last_id, 'HCM3.StatuteFactorProperty'),
    )

    print(
        f"  -> Formulas inserted: {formulas_inserted}. "
        f"Properties inserted: {properties_inserted}. "
        f"Skipped (no factor map): {skipped_no_factor}. "
        f"Source versions: {len(versions_df)}. "
        f"ETs each: {len(et_ids)}."
    )


def run():
    print("\n--- Running Step 17: Statute Factor Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_statute_factor_mapping_table(dest_cursor)
        # Keep legacy property mapping until clear can use it to delete old rows
        setup_statute_factor_property_mapping_table(
            dest_cursor, recreate_if_legacy=False
        )

        _migrate_factor_masters(source_cnxn, dest_cnxn, dest_cursor)
        _migrate_factor_properties(source_cnxn, dest_cnxn, dest_cursor)

        dest_cnxn.commit()
        print("Success! Statute factor masters/properties/formulas migration finished.")

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
