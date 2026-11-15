"""Step 11: Migrate HRS_RuleDocument → HCM3.EmployeeStatute."""
import pandas as pd
import pyodbc
import warnings
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian
from utils.date_helpers import gregorian_to_shamsi_year_month, shamsi_to_gregorian
from utils.hcm_extra_settings import ensure_hcm_extra_fields
from utils.lookup_helpers import ensure_lookup_codes
from utils.org_migration import (
    ensure_departments,
    ensure_employment_types,
    ensure_jobs,
    ensure_places_as_work_locations,
    ensure_posts,
    ensure_rank_codes_from_grades,
    ensure_table_id,
    setup_statute_mapping_table,
    setup_statute_type_mapping_table,
    rank1_job_by_parent,
)

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'
# SYS3.Lookup EmployeeStatuteStatus: 1 ذخیره, 5 تایید کارگزینی, 10 تایید پرداخت
STATUTE_STATUS_PAYMENT_CONFIRMED = 10

# StatuteType required defaults (Rahkaran NOT NULL columns).
# Migrated statutes copy IssueTimeCode and AfterIssueStatusCode from the type.
# HRS_RdPersonalGrade 1-20 → Extra1, caption رتبه. Same list on both screens.
# HRS_RdAbetGrade 1-5 is one column. The جاری screen calls it گروه تشویقی
# (Extra2) and the خدمات کشوری screen calls it طبقه تشویقی (Extra4).
# Which screen a row belongs to follows HRS_RdJobGrade: a plain 1-20 is جاری,
# a PayBase parent-68 class is خدمات کشوری.
# HRS_RdJobGrade plain numbers 1-20 → Extra3, caption گروه استحقاقی.
# PayBase parent 68 → Rank, caption طبقه شغلی: مقدماتی..عالی.
STATUTE_EXTRA1_LOOKUP = 'EmployeeStatuteExtra1'
STATUTE_EXTRA2_LOOKUP = 'EmployeeStatuteExtra2'
STATUTE_EXTRA3_LOOKUP = 'EmployeeStatuteExtra3'
STATUTE_EXTRA4_LOOKUP = 'EmployeeStatuteExtra4'
STATUTE_RANK_LOOKUP = 'Rank'
STATUTE_RANK_VALUES = {
    1: 'مقدماتی',
    2: 'پایه',
    3: 'ارشد',
    4: 'خبره',
    5: 'عالی',
}
STATUTE_EXTRA1_VALUES = {code: str(code) for code in range(1, 21)}
STATUTE_EXTRA2_VALUES = {code: str(code) for code in range(1, 6)}
STATUTE_EXTRA3_VALUES = {code: str(code) for code in range(1, 21)}
STATUTE_EXTRA4_VALUES = {code: str(code) for code in range(1, 6)}
JOB_CLASS_TO_RANK = {
    680001: 1,
    680002: 2,
    680003: 3,
    680004: 4,
    680005: 5,
}

STATUTE_TYPE_DEFAULTS = {
    'IssueTimeCode': 1,               # در حین استخدام
    'AfterIssueStatusCode': 1,
    'PostSelectionTypeCode': 1,       # انتخاب از ساختار
    'TempPostSelectionTypeCode': 1,
    'Status': 1,
    'RelatedStatuteTypeCode': 1,
}
# The app treats IssueTimeCode 2 (در شروع استخدام) as the employment statute.
# Each employee's earliest statute uses this type so later statutes can
# inherit employment date and the other parameters from it.
START_EMPLOYMENT_STATUTE_TITLE = 'شروع استخدام استقرار سیستم'
START_EMPLOYMENT_ISSUE_TIME = 2
# StatuteTypeParameter rows the app wrote for the sample employment type.
# SetMethodCode 2 is مقدار حکم قبلی. TypeCode 2 is the only one not required.
STATUTE_TYPE_PARAMETER_ROWS = tuple(
    (type_code, True, 2, type_code != 2) for type_code in range(1, 14)
)


def _parse_shamsi_date(raw, *, treat_open_end_as_null=False):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    date_part = text.split()[0]
    if date_part in ('', '0', '____/__/__', '/  /', '//', '0/0/0'):
        return None
    if treat_open_end_as_null and date_part == OPEN_END_SHAMSI:
        return None
    if '_' in date_part or date_part.count('/') != 2:
        return None
    return shamsi_to_gregorian(date_part)


def _positive_fk(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = int(float(val))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _statute_title_key(title):
    """
    Fold title for uniqueness matching SQL Arabic collation quirks
    (tatweel/kashida, trailing dots, spacing).
    """
    text = normalize_persian(title) if title else '-'
    text = text.replace('\u0640', '')  # Arabic tatweel ـ
    text = text.replace('.', '')
    text = ' '.join(text.split())
    return text


def _statute_display_title(title):
    """Canonical title stored in dest (no tatweel / trailing dots)."""
    text = title or '-'
    text = text.replace('\u0640', '')
    text = text.rstrip('.').strip()
    text = ' '.join(text.split())
    return text[:400]


def ensure_statute_types(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate HRS_RuleType → HCM3.StatuteType. Returns SourceRtID → DestStatuteTypeID."""
    setup_statute_type_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            HRS_RtID AS SourceRuleTypeID,
            HRS_RtRuleName AS RuleName,
            HRS_RtSystemCode AS RuleCode,
            HRS_RtActive AS RuleActive,
            HRS_RtDescription AS RuleDescription
        FROM dbo.HRS_RuleType
        WHERE HRS_RtID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourceRuleTypeID, DestStatuteTypeID "
        "FROM master.dbo.StatuteTypeMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(r['SourceRuleTypeID']): int(r['DestStatuteTypeID'])
        for _, r in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source rule types found.")
        return result

    missing_df = source_df[~source_df['SourceRuleTypeID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Statute types already mapped: {len(result)}.")
        return result

    existing_titles_df = pd.read_sql(
        "SELECT StatuteTypeID, Title FROM HCM3.StatuteType",
        dest_cnxn,
    )
    title_key_to_id = {}
    for _, row in existing_titles_df.iterrows():
        raw = str(row['Title']) if row['Title'] is not None else '-'
        title_key_to_id[_statute_title_key(raw)] = int(row['StatuteTypeID'])

    last_id = ensure_table_id(dest_cursor, 'HCM3.StatuteType', 0)
    insert_sql = """
        INSERT INTO HCM3.StatuteType (
            StatuteTypeID, Code, Title,
            IssueTimeCode, AfterIssueStatusCode, PostSelectionTypeCode,
            TempPostSelectionTypeCode, Status, RelatedStatuteTypeCode,
            DefaultDescription,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.StatuteTypeMigrationMapping (
            SourceRuleTypeID, DestStatuteTypeID
        ) VALUES (?, ?)
    """

    inserted = 0
    reused = 0
    uniquified = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourceRuleTypeID'])
        title = clean_persian_text(row['RuleName']) or '-'
        display_title = _statute_display_title(title)
        title_key = _statute_title_key(display_title)

        if title_key in title_key_to_id:
            dest_id = title_key_to_id[title_key]
            dest_cursor.execute(insert_mapping_sql, (source_id, dest_id))
            result[source_id] = dest_id
            reused += 1
            continue

        dest_cursor.execute(
            "SELECT TOP 1 StatuteTypeID FROM HCM3.StatuteType WHERE Title = ?",
            (display_title,),
        )
        existing_row = dest_cursor.fetchone()
        if existing_row:
            dest_id = int(existing_row[0])
            dest_cursor.execute(insert_mapping_sql, (source_id, dest_id))
            title_key_to_id[title_key] = dest_id
            result[source_id] = dest_id
            reused += 1
            continue

        code_raw = row['RuleCode']
        code = None
        if code_raw is not None and not (isinstance(code_raw, float) and pd.isna(code_raw)):
            code = str(code_raw).strip()[:100] or None
        if not code:
            code = str(source_id)

        insert_title = display_title
        # Final guarantee for unique index
        suffix = f"-{source_id}"
        insert_title = f"{insert_title[:max(0, 400 - len(suffix))]}{suffix}"
        uniquified += 1

        last_id += 1
        dest_cursor.execute(
            insert_sql,
            (
                last_id,
                code,
                insert_title,
                STATUTE_TYPE_DEFAULTS['IssueTimeCode'],
                STATUTE_TYPE_DEFAULTS['AfterIssueStatusCode'],
                STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['TempPostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['Status'],
                STATUTE_TYPE_DEFAULTS['RelatedStatuteTypeCode'],
                clean_persian_text(row['RuleDescription']),
            ),
        )
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        title_key_to_id[title_key] = last_id
        title_key_to_id[_statute_title_key(insert_title)] = last_id
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.StatuteType'",
        (last_id,),
    )
    print(
        f"  -> Statute types inserted: {inserted}, reused by title: {reused}, "
        f"titles uniquified: {uniquified}. Total mapped: {len(result)}."
    )
    return result


def _statute_type_descriptions(source_cnxn, dest_cnxn):
    """Longest source description for each migrated statute type."""
    source_df = pd.read_sql(
        """
        SELECT HRS_RtID AS SourceRuleTypeID, HRS_RtDescription AS RuleDescription
        FROM dbo.HRS_RuleType
        WHERE HRS_RtID > 0
        """,
        source_cnxn,
    )
    mapping_df = pd.read_sql(
        """
        SELECT SourceRuleTypeID, DestStatuteTypeID
        FROM master.dbo.StatuteTypeMigrationMapping
        """,
        dest_cnxn,
    )
    if source_df.empty or mapping_df.empty:
        return []
    merged = mapping_df.merge(source_df, on='SourceRuleTypeID', how='inner')
    merged['Description'] = merged['RuleDescription'].map(clean_persian_text)
    merged = merged[merged['Description'].notna()].copy()
    if merged.empty:
        return []
    merged['DescriptionLength'] = merged['Description'].str.len()
    best = (
        merged.sort_values(
            ['DescriptionLength', 'SourceRuleTypeID'],
            ascending=[False, True],
        )
        .drop_duplicates('DestStatuteTypeID', keep='first')
    )
    return [
        (str(row.Description), int(row.DestStatuteTypeID))
        for row in best.itertuples(index=False)
    ]


def _ensure_start_employment_statute_type(dest_cursor):
    """The employment statute type. Issue time is در شروع استخدام."""
    dest_cursor.execute(
        "SELECT StatuteTypeID FROM HCM3.StatuteType WHERE Title = ?",
        (START_EMPLOYMENT_STATUTE_TITLE,),
    )
    row = dest_cursor.fetchone()
    if row:
        statute_type_id = int(row[0])
        dest_cursor.execute(
            """
            UPDATE HCM3.StatuteType
            SET IssueTimeCode = ?,
                AfterIssueStatusCode = ?,
                PostSelectionTypeCode = ?,
                TempPostSelectionTypeCode = ?,
                Status = ?,
                RelatedStatuteTypeCode = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE StatuteTypeID = ?
              AND (
                IssueTimeCode <> ?
                OR PostSelectionTypeCode <> ?
                OR AfterIssueStatusCode <> ?
                OR TempPostSelectionTypeCode <> ?
                OR Status <> ?
                OR RelatedStatuteTypeCode <> ?
              )
            """,
            (
                START_EMPLOYMENT_ISSUE_TIME,
                STATUTE_TYPE_DEFAULTS['AfterIssueStatusCode'],
                STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['TempPostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['Status'],
                STATUTE_TYPE_DEFAULTS['RelatedStatuteTypeCode'],
                statute_type_id,
                START_EMPLOYMENT_ISSUE_TIME,
                STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['AfterIssueStatusCode'],
                STATUTE_TYPE_DEFAULTS['TempPostSelectionTypeCode'],
                STATUTE_TYPE_DEFAULTS['Status'],
                STATUTE_TYPE_DEFAULTS['RelatedStatuteTypeCode'],
            ),
        )
        return statute_type_id

    last_id = ensure_table_id(dest_cursor, 'HCM3.StatuteType', 0) + 1
    dest_cursor.execute(
        """
        INSERT INTO HCM3.StatuteType (
            StatuteTypeID, Code, Title,
            IssueTimeCode, AfterIssueStatusCode, PostSelectionTypeCode,
            TempPostSelectionTypeCode, Status, RelatedStatuteTypeCode,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """,
        (
            last_id,
            START_EMPLOYMENT_STATUTE_TITLE,
            START_EMPLOYMENT_ISSUE_TIME,
            STATUTE_TYPE_DEFAULTS['AfterIssueStatusCode'],
            STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
            STATUTE_TYPE_DEFAULTS['TempPostSelectionTypeCode'],
            STATUTE_TYPE_DEFAULTS['Status'],
            STATUTE_TYPE_DEFAULTS['RelatedStatuteTypeCode'],
        ),
    )
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.StatuteType'",
        (last_id,),
    )
    return last_id


def _ensure_statute_type_parameters(dest_cursor):
    """Copy the sample parameter pattern onto every statute type that lacks it."""
    last_id = ensure_table_id(dest_cursor, 'HCM3.StatuteTypeParameter', 0)
    dest_cursor.execute(
        """
        IF OBJECT_ID('tempdb..#StatuteTypeParameterSeed') IS NOT NULL
            DROP TABLE #StatuteTypeParameterSeed
        CREATE TABLE #StatuteTypeParameterSeed (
            TypeCode INT NOT NULL PRIMARY KEY,
            Editable BIT NOT NULL,
            SetMethodCode INT NOT NULL,
            Required BIT NOT NULL
        )
        """
    )
    dest_cursor.fast_executemany = True
    dest_cursor.executemany(
        """
        INSERT INTO #StatuteTypeParameterSeed
            (TypeCode, Editable, SetMethodCode, Required)
        VALUES (?, ?, ?, ?)
        """,
        list(STATUTE_TYPE_PARAMETER_ROWS),
    )
    dest_cursor.execute(
        """
        SET NOCOUNT ON;
        DECLARE @base BIGINT = ?;
        INSERT INTO HCM3.StatuteTypeParameter (
            StatuteTypeParameterID, StatuteTypeRef, TypeCode,
            Editable, SetMethodCode, Required,
            CreationDate, Creator, LastModificationDate, LastModifier
        )
        SELECT
            @base + ROW_NUMBER() OVER (ORDER BY t.StatuteTypeID, p.TypeCode),
            t.StatuteTypeID,
            p.TypeCode,
            p.Editable,
            p.SetMethodCode,
            p.Required,
            GETDATE(), 1, GETDATE(), 1
        FROM HCM3.StatuteType t
        CROSS JOIN #StatuteTypeParameterSeed p
        WHERE NOT EXISTS (
            SELECT 1
            FROM HCM3.StatuteTypeParameter existing
            WHERE existing.StatuteTypeRef = t.StatuteTypeID
              AND existing.TypeCode = p.TypeCode
        );
        DECLARE @inserted INT = @@ROWCOUNT;
        UPDATE SYS3.tableIdGen
        SET LastId = @base + @inserted
        WHERE TableName = 'HCM3.StatuteTypeParameter';
        SELECT @inserted;
        """,
        (last_id,),
    )
    inserted = int(dest_cursor.fetchone()[0])
    print(f"  -> Statute type parameters inserted: {inserted}.")


def _ensure_statute_type_factors(dest_cursor):
    """
    One editable StatuteTypeFactor for every employment type and factor that
    has a property. That is the set the sample employment type holds.
    """
    last_id = ensure_table_id(dest_cursor, 'HCM3.StatuteTypeFactor', 0)
    dest_cursor.execute(
        """
        SET NOCOUNT ON;
        DECLARE @base BIGINT = ?;
        INSERT INTO HCM3.StatuteTypeFactor (
            StatuteTypeFactorID, StatuteTypeRef, EmploymentTypeRef,
            StatuteFactorRef, Editable,
            CreationDate, Creator, LastModificationDate, LastModifier
        )
        SELECT
            @base + ROW_NUMBER() OVER (
                ORDER BY t.StatuteTypeID, pair.EmploymentTypeRef, pair.StatuteFactorRef
            ),
            t.StatuteTypeID,
            pair.EmploymentTypeRef,
            pair.StatuteFactorRef,
            1,
            GETDATE(), 1, GETDATE(), 1
        FROM HCM3.StatuteType t
        CROSS JOIN (
            SELECT DISTINCT EmploymentTypeRef, StatuteFactorRef
            FROM HCM3.StatuteFactorProperty
            WHERE EmploymentTypeRef IS NOT NULL
              AND StatuteFactorRef IS NOT NULL
        ) pair
        WHERE NOT EXISTS (
            SELECT 1
            FROM HCM3.StatuteTypeFactor existing
            WHERE existing.StatuteTypeRef = t.StatuteTypeID
              AND existing.EmploymentTypeRef = pair.EmploymentTypeRef
              AND existing.StatuteFactorRef = pair.StatuteFactorRef
        );
        DECLARE @inserted INT = @@ROWCOUNT;
        UPDATE SYS3.tableIdGen
        SET LastId = @base + @inserted
        WHERE TableName = 'HCM3.StatuteTypeFactor';
        SELECT @inserted;
        """,
        (last_id,),
    )
    inserted = int(dest_cursor.fetchone()[0])
    print(f"  -> Statute type factors inserted: {inserted}.")


def _assign_start_statute_type(dest_cursor, start_type_id):
    """Point each employee's earliest migrated statute at the employment type."""
    dest_cursor.execute(
        """
        UPDATE s
        SET s.StatuteTypeRef = ?,
            s.IssueTimeCode = ?,
            s.LastModificationDate = GETDATE(),
            s.LastModifier = 1
        FROM HCM3.EmployeeStatute s
        INNER JOIN (
            SELECT EmployeeStatuteID
            FROM (
                SELECT
                    es.EmployeeStatuteID,
                    ROW_NUMBER() OVER (
                        PARTITION BY es.EmployeeRef
                        ORDER BY
                            CASE WHEN es.ApplyDate IS NULL THEN 1 ELSE 0 END,
                            es.ApplyDate,
                            es.IssueDate,
                            es.EmployeeStatuteID
                    ) AS rn
                FROM HCM3.EmployeeStatute es
                INNER JOIN master.dbo.StatuteMigrationMapping m
                    ON m.DestEmployeeStatuteID = es.EmployeeStatuteID
            ) ranked
            WHERE ranked.rn = 1
        ) first_statute ON first_statute.EmployeeStatuteID = s.EmployeeStatuteID
        WHERE ISNULL(s.StatuteTypeRef, -1) <> ?
           OR ISNULL(s.IssueTimeCode, -1) <> ?
        """,
        (
            start_type_id,
            START_EMPLOYMENT_ISSUE_TIME,
            start_type_id,
            START_EMPLOYMENT_ISSUE_TIME,
        ),
    )
    print(f"  -> Earliest statutes set to the employment type: {dest_cursor.rowcount}.")


def ensure_statute_type_profile(source_cnxn, dest_cnxn, dest_cursor):
    """
    Descriptions, post selection, parameters, and factor rows on statute types.
    The earliest statute of each employee becomes the employment statute type.
    """
    print("Updating statute type descriptions and post selection...")
    descriptions = _statute_type_descriptions(source_cnxn, dest_cnxn)
    dest_cursor.execute(
        """
        IF OBJECT_ID('tempdb..#StatuteTypeDescription') IS NOT NULL
            DROP TABLE #StatuteTypeDescription
        CREATE TABLE #StatuteTypeDescription (
            DestStatuteTypeID BIGINT NOT NULL PRIMARY KEY,
            Description NVARCHAR(MAX) NULL
        )
        """
    )
    if descriptions:
        dest_cursor.fast_executemany = False
        dest_cursor.setinputsizes([(pyodbc.SQL_WVARCHAR, 4000, 0), None])
        dest_cursor.executemany(
            """
            INSERT INTO #StatuteTypeDescription (Description, DestStatuteTypeID)
            VALUES (?, ?)
            """,
            descriptions,
        )
    dest_cursor.execute(
        """
        UPDATE t
        SET t.PostSelectionTypeCode = ?,
            t.DefaultDescription = d.Description,
            t.LastModificationDate = GETDATE(),
            t.LastModifier = 1
        FROM HCM3.StatuteType t
        INNER JOIN (
            SELECT DISTINCT DestStatuteTypeID
            FROM master.dbo.StatuteTypeMigrationMapping
        ) mapped ON mapped.DestStatuteTypeID = t.StatuteTypeID
        LEFT JOIN #StatuteTypeDescription d
            ON d.DestStatuteTypeID = t.StatuteTypeID
        WHERE t.PostSelectionTypeCode <> ?
           OR ISNULL(t.DefaultDescription, N'') COLLATE DATABASE_DEFAULT
              <> ISNULL(d.Description, N'') COLLATE DATABASE_DEFAULT
        """,
        (
            STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
            STATUTE_TYPE_DEFAULTS['PostSelectionTypeCode'],
        ),
    )
    print(f"  -> Statute types updated: {dest_cursor.rowcount}.")

    start_type_id = _ensure_start_employment_statute_type(dest_cursor)
    print(f"  -> Employment statute type: {start_type_id}.")
    _ensure_statute_type_parameters(dest_cursor)
    _ensure_statute_type_factors(dest_cursor)
    _assign_start_statute_type(dest_cursor, start_type_id)


def _resolve_structure_ref(
    source_post_id,
    dest_dept_id,
    apply_date,
    post_oc_map,
    structure_map,
    structure_meta,
):
    """
    Find OrgStructure node for (post, its Oc) when dest department matches
    and apply_date is within Insertion/Deletion window.
    """
    if not source_post_id or not dest_dept_id:
        return None
    oc_id = post_oc_map.get(source_post_id)
    if oc_id is None:
        return None
    structure_id = structure_map.get((source_post_id, oc_id))
    if not structure_id:
        return None
    meta = structure_meta.get(structure_id)
    if not meta:
        return structure_id
    struct_dept, insertion, deletion = meta
    if struct_dept != dest_dept_id:
        return None
    if apply_date and insertion and str(apply_date) < str(insertion)[:10]:
        return None
    if apply_date and deletion and str(apply_date) > str(deletion)[:10]:
        return None
    return structure_id


def _job_class_rank(raw):
    """Map خدمات کشوری classes onto Rank (طبقه شغلی). Other values stay empty."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        source_id = int(float(raw))
    except (TypeError, ValueError):
        return None
    return JOB_CLASS_TO_RANK.get(source_id)


def _entitlement_group_extra3(raw):
    """Map جاری گروه استحقاقی numbers onto Extra3. Class ids stay empty."""
    code = _positive_fk(raw)
    if code is None or code > 20:
        return None
    return code


def _encouragement_code(raw):
    """HRS_RdAbetGrade 1-5. Zero and anything outside 1-5 stay empty."""
    code = _positive_fk(raw)
    if code is None or code > 5:
        return None
    return code


def _personal_grade_extra1(raw):
    """Map رتبه 1-20 onto Extra1. Zero and anything outside 1-20 stay empty."""
    code = _positive_fk(raw)
    if code is None or code > 20:
        return None
    return code


def _screen_encouragement(job_grade, abet_grade):
    """
    Same stored number, two screen captions.
    A plain job-grade number is the جاری screen (گروه تشویقی → Extra2).
    A class id is the خدمات کشوری screen (طبقه تشویقی → Extra4).
    """
    code = _encouragement_code(abet_grade)
    if code is None:
        return None, None
    if _entitlement_group_extra3(job_grade) is not None:
        return code, None
    if _job_class_rank(job_grade) is not None:
        return None, code
    return None, None


def _statute_grade_rows(source_cnxn, dest_cursor):
    """Source grades joined to migrated statutes. The source database name is not fixed."""
    grades = pd.read_sql(
        """
        SELECT
            HRS_RdID AS SourceRuleDocumentID,
            HRS_RdJobGrade AS JobGrade,
            HRS_RdPersonalGrade AS PersonalGrade,
            HRS_RdAbetGrade AS AbetGrade
        FROM dbo.HRS_RuleDocument
        WHERE HRS_RdID > 0
        """,
        source_cnxn,
    )
    grade_by_source = {}
    for row in grades.itertuples(index=False):
        group_encouragement, class_encouragement = _screen_encouragement(
            row.JobGrade, row.AbetGrade
        )
        grade_by_source[int(row.SourceRuleDocumentID)] = (
            _job_class_rank(row.JobGrade),
            _personal_grade_extra1(row.PersonalGrade),
            group_encouragement,
            _entitlement_group_extra3(row.JobGrade),
            class_encouragement,
        )
    dest_cursor.execute(
        """
        SELECT SourceRuleDocumentID, DestEmployeeStatuteID
        FROM master.dbo.StatuteMigrationMapping
        """
    )
    rows = []
    for source_id, dest_id in dest_cursor.fetchall():
        rank_code, extra1_code, extra2_code, extra3_code, extra4_code = (
            grade_by_source.get(int(source_id), (None, None, None, None, None))
        )
        rows.append((
            int(dest_id), rank_code, extra1_code, extra2_code, extra3_code,
            extra4_code,
        ))
    return rows


def _apply_statute_rank_extra_dates_and_type_codes(source_cnxn, dest_cnxn, dest_cursor):
    """
    On migrated statutes:
    Extra1 is رتبه (1-20). Extra2 is گروه تشویقی. Extra3 is گروه استحقاقی.
    Extra4 is طبقه تشویقی. Rank stays the five طبقه شغلی classes.
    issue/apply year-months come from the dates, confirm dates follow the
    issue date, and issue-time / after-issue status are copied from the type.
    Work records that were copied from a statute take the statute's new rank.
    """
    print("Ensuring Extra1 (رتبه), Extra2 (گروه تشویقی), Extra3 (گروه استحقاقی), Extra4 (طبقه تشویقی)...")
    ensure_lookup_codes(
        dest_cnxn,
        dest_cursor,
        STATUTE_RANK_LOOKUP,
        STATUTE_RANK_VALUES,
        overwrite_values=True,
    )
    ensure_lookup_codes(
        dest_cnxn,
        dest_cursor,
        STATUTE_EXTRA1_LOOKUP,
        STATUTE_EXTRA1_VALUES,
        overwrite_values=True,
    )
    ensure_lookup_codes(
        dest_cnxn,
        dest_cursor,
        STATUTE_EXTRA2_LOOKUP,
        STATUTE_EXTRA2_VALUES,
        overwrite_values=True,
    )
    ensure_lookup_codes(
        dest_cnxn,
        dest_cursor,
        STATUTE_EXTRA3_LOOKUP,
        STATUTE_EXTRA3_VALUES,
        overwrite_values=True,
    )
    ensure_lookup_codes(
        dest_cnxn,
        dest_cursor,
        STATUTE_EXTRA4_LOOKUP,
        STATUTE_EXTRA4_VALUES,
        overwrite_values=True,
    )
    ensure_hcm_extra_fields(
        dest_cursor,
        (
            STATUTE_RANK_LOOKUP,
            STATUTE_EXTRA1_LOOKUP,
            STATUTE_EXTRA2_LOOKUP,
            STATUTE_EXTRA3_LOOKUP,
            STATUTE_EXTRA4_LOOKUP,
        ),
    )
    grade_rows = _statute_grade_rows(source_cnxn, dest_cursor)
    dest_cursor.execute(
        """
        IF OBJECT_ID('tempdb..#StatuteGrade') IS NOT NULL DROP TABLE #StatuteGrade
        CREATE TABLE #StatuteGrade (
            DestEmployeeStatuteID BIGINT NOT NULL PRIMARY KEY,
            RankCode INT NULL,
            Extra1Code INT NULL,
            Extra2Code INT NULL,
            Extra3Code INT NULL,
            Extra4Code INT NULL
        )
        """
    )
    if grade_rows:
        dest_cursor.fast_executemany = True
        dest_cursor.executemany(
            """
            INSERT INTO #StatuteGrade (
                DestEmployeeStatuteID, RankCode, Extra1Code, Extra2Code,
                Extra3Code, Extra4Code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            grade_rows,
        )
    dest_cursor.execute(
        """
        UPDATE s
        SET s.RankCode = x.RankCode,
            s.Extra1Code = x.Extra1Code,
            s.Extra2Code = x.Extra2Code,
            s.Extra3Code = x.Extra3Code,
            s.Extra4Code = x.Extra4Code,
            s.LastModificationDate = GETDATE(),
            s.LastModifier = 1
        FROM HCM3.EmployeeStatute s
        INNER JOIN #StatuteGrade x
            ON x.DestEmployeeStatuteID = s.EmployeeStatuteID
        WHERE ISNULL(s.RankCode, -1) <> ISNULL(x.RankCode, -1)
           OR ISNULL(s.Extra1Code, -1) <> ISNULL(x.Extra1Code, -1)
           OR ISNULL(s.Extra2Code, -1) <> ISNULL(x.Extra2Code, -1)
           OR ISNULL(s.Extra3Code, -1) <> ISNULL(x.Extra3Code, -1)
           OR ISNULL(s.Extra4Code, -1) <> ISNULL(x.Extra4Code, -1)
        """
    )
    print(f"  -> Statute extras updated: {dest_cursor.rowcount}.")

    dest_cursor.execute(
        """
        UPDATE wr
        SET wr.RankCode = s.RankCode,
            wr.LastModificationDate = GETDATE(),
            wr.LastModifier = 1
        FROM HCM3.EmployeeWorkRecord wr
        INNER JOIN HCM3.EmployeeStatute s
            ON s.EmployeeRef = wr.EmployeeRef
           AND ISNULL(s.Number, N'') = ISNULL(wr.StatuteNumber, N'')
           AND (
                (s.ApplyDate IS NULL AND wr.StatuteApplyDate IS NULL)
                OR CAST(s.ApplyDate AS date) = CAST(wr.StatuteApplyDate AS date)
           )
        INNER JOIN master.dbo.StatuteMigrationMapping m
            ON m.DestEmployeeStatuteID = s.EmployeeStatuteID
        WHERE ISNULL(wr.RankCode, -1) <> ISNULL(s.RankCode, -1)
        """
    )
    print(f"  -> Work-record Rank aligned to statute طبقه شغلی: {dest_cursor.rowcount}.")

    print("Copying statute-type codes and filling statute year-months...")
    dest_cursor.execute(
        """
        SELECT
            s.EmployeeStatuteID,
            s.IssueDate,
            s.ApplyDate,
            t.IssueTimeCode,
            t.AfterIssueStatusCode
        FROM HCM3.EmployeeStatute s
        INNER JOIN master.dbo.StatuteMigrationMapping m
            ON m.DestEmployeeStatuteID = s.EmployeeStatuteID
        LEFT JOIN HCM3.StatuteType t ON t.StatuteTypeID = s.StatuteTypeRef
        """
    )
    pending = []
    for statute_id, issue_date, apply_date, issue_time, after_status in dest_cursor.fetchall():
        issue_ym = gregorian_to_shamsi_year_month(issue_date)
        pending.append(
            (
                int(issue_time) if issue_time is not None else None,
                int(after_status) if after_status is not None else None,
                issue_ym,
                gregorian_to_shamsi_year_month(apply_date),
                issue_date,
                issue_ym,
                issue_date,
                int(statute_id),
            )
        )
    if pending:
        dest_cursor.fast_executemany = True
        dest_cursor.executemany(
            """
            UPDATE HCM3.EmployeeStatute
            SET IssueTimeCode = ?,
                AfterIssueStatusCode = ?,
                IssueYearMonth = ?,
                ApplyYearMonth = ?,
                ConfirmDate = ?,
                ConfirmYearMonth = ?,
                PaymentConfirmDate = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeStatuteID = ?
            """,
            pending,
        )
    print(f"  -> Statute codes and dates updated: {len(pending)}.")


def _fill_employment_date_and_payment_status(source_cnxn, dest_cnxn, dest_cursor):
    """
    EmploymentDate on every statute is TBL_PersonnelEmployeDate when that
    Shamsi date parses. Otherwise it is the employee's first statute date:
    the earliest ApplyDate, or the earliest IssueDate when no apply date exists.
    Status is تایید پرداخت.
    """
    source_df = pd.read_sql(
        """
        SELECT
            TBL_PersonnelID AS SourceID,
            TBL_PersonnelEmployeDate AS EmployDate
        FROM dbo.TBL_Personnel
        """,
        source_cnxn,
    )
    employ_dates = {}
    for row in source_df.itertuples(index=False):
        parsed = shamsi_to_gregorian(row.EmployDate)
        if parsed:
            employ_dates[int(row.SourceID)] = parsed

    emp_df = pd.read_sql(
        "SELECT EmployeeID, Code FROM HCM3.Employee",
        dest_cnxn,
    )
    rows = []
    for row in emp_df.itertuples(index=False):
        code = '' if row.Code is None else str(row.Code).strip()
        if not code:
            continue
        try:
            source_id = int(float(code))
        except (TypeError, ValueError):
            continue
        parsed = employ_dates.get(source_id)
        if parsed:
            rows.append((int(row.EmployeeID), parsed))

    dest_cursor.execute(
        """
        IF OBJECT_ID('tempdb..#PersonnelEmployDate') IS NOT NULL
            DROP TABLE #PersonnelEmployDate
        CREATE TABLE #PersonnelEmployDate (
            EmployeeID BIGINT NOT NULL PRIMARY KEY,
            EmployDate DATETIME NOT NULL
        )
        """
    )
    if rows:
        dest_cursor.fast_executemany = True
        dest_cursor.executemany(
            "INSERT INTO #PersonnelEmployDate (EmployeeID, EmployDate) VALUES (?, ?)",
            rows,
        )
    dest_cursor.execute(
        """
        UPDATE s
        SET s.EmploymentDate = COALESCE(x.EmployDate, d.FirstDate),
            s.Status = ?,
            s.LastModificationDate = GETDATE(),
            s.LastModifier = 1
        FROM HCM3.EmployeeStatute s
        INNER JOIN (
            SELECT
                EmployeeRef,
                COALESCE(MIN(ApplyDate), MIN(IssueDate)) AS FirstDate
            FROM HCM3.EmployeeStatute
            GROUP BY EmployeeRef
        ) d ON d.EmployeeRef = s.EmployeeRef
        LEFT JOIN #PersonnelEmployDate x ON x.EmployeeID = s.EmployeeRef
        """,
        (STATUTE_STATUS_PAYMENT_CONFIRMED,),
    )
    print(
        "  -> Employment date set from the personnel employment date "
        f"({len(rows)} employees), otherwise the first statute date. "
        f"Payment-confirmed status set on {dest_cursor.rowcount} statutes."
    )


def run():
    print("\n--- Running Step 11: Employee Statute Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_statute_mapping_table(dest_cursor)

        print("Ensuring masters...")
        dept_map = ensure_departments(source_cnxn, dest_cnxn, dest_cursor)
        post_map = ensure_posts(source_cnxn, dest_cnxn, dest_cursor)
        job_map = ensure_jobs(source_cnxn, dest_cnxn, dest_cursor)
        # Statutes (like PostJob) point at rank-1 children, not migrated parents.
        rank1_by_parent = rank1_job_by_parent(dest_cnxn, set(job_map.values()))
        et_map = ensure_employment_types(source_cnxn, dest_cnxn, dest_cursor)
        place_map = ensure_places_as_work_locations(source_cnxn, dest_cnxn, dest_cursor)

        # RuleDocument has no Job FK — resolve via post's TBL_JobID_fk
        post_job_df = pd.read_sql("""
            SELECT
                TBL_PostID AS SourcePostID,
                TBL_JobID_fk AS SourceJobID
            FROM dbo.TBL_Post
            WHERE TBL_PostID > 0
              AND TBL_JobID_fk IS NOT NULL
              AND TBL_JobID_fk > 0
        """, source_cnxn)
        post_to_job = {
            int(r['SourcePostID']): int(r['SourceJobID'])
            for _, r in post_job_df.iterrows()
        }

        print("Ensuring Statute Types from RuleType...")
        statute_type_map = ensure_statute_types(source_cnxn, dest_cnxn, dest_cursor)
        type_code_df = pd.read_sql(
            """
            SELECT StatuteTypeID, IssueTimeCode, AfterIssueStatusCode
            FROM HCM3.StatuteType
            """,
            dest_cnxn,
        )
        type_codes = {
            int(r['StatuteTypeID']): (
                int(r['IssueTimeCode']) if pd.notna(r['IssueTimeCode']) else None,
                int(r['AfterIssueStatusCode']) if pd.notna(r['AfterIssueStatusCode']) else None,
            )
            for _, r in type_code_df.iterrows()
        }

        print("Loading org-structure mappings...")
        struct_map_df = pd.read_sql("""
            SELECT SourceID AS SourcePostID, SourceOcID, DestOrganizationalStructureID
            FROM master.dbo.OrgStructureMigrationMapping
            WHERE NodeKind = 'P'
        """, dest_cnxn)
        structure_map = {
            (int(r['SourcePostID']), int(r['SourceOcID'])): int(r['DestOrganizationalStructureID'])
            for _, r in struct_map_df.iterrows()
        } if not struct_map_df.empty else {}

        structure_meta = {}
        if structure_map:
            ids = list(structure_map.values())
            # Load in chunks if huge
            meta_df = pd.read_sql("""
                SELECT OrganizationalStructureID, DepartmentRef, InsertionDate, DeletionDate
                FROM HCM3.OrganizationalStructure
            """, dest_cnxn)
            structure_meta = {
                int(r['OrganizationalStructureID']): (
                    int(r['DepartmentRef']) if r['DepartmentRef'] is not None else None,
                    str(r['InsertionDate'])[:10] if r['InsertionDate'] is not None else None,
                    str(r['DeletionDate'])[:10] if r['DeletionDate'] is not None else None,
                )
                for _, r in meta_df.iterrows()
                if int(r['OrganizationalStructureID']) in set(ids)
            }

        post_oc_df = pd.read_sql("""
            SELECT TBL_PostID AS SourcePostID, TBL_OcID_fk AS SourceOcID
            FROM dbo.TBL_Post
            WHERE TBL_PostID > 0 AND TBL_OcID_fk > 0
        """, source_cnxn)
        post_oc_map = {
            int(r['SourcePostID']): int(r['SourceOcID'])
            for _, r in post_oc_df.iterrows()
        }

        print("Fetching active RuleDocuments...")
        rd_df = pd.read_sql("""
            SELECT
                rd.HRS_RdID AS SourceRuleDocumentID,
                rd.TBL_PersonnelID_fk AS SourceID,
                rd.TBL_EtID_fk AS SourceEmploymentTypeID,
                rd.TBL_PlaceID_fk AS SourcePlaceID,
                rd.TBL_DepartmentID_fk AS SourceDepartmentID,
                rd.TBL_PostID_fk AS SourcePostID,
                rd.HRS_RtID_fk AS SourceRuleTypeID,
                rd.HRS_RdRuleNo AS RuleNo,
                rd.HRS_RdExportDate AS ExportDate,
                rd.HRS_RdExcuteDate AS ExecuteDate,
                rd.HRS_RdEndDate AS EndDate,
                rd.HRS_RdContractEndDate AS ContractEndDate,
                rd.HRS_RdPersonalGrade AS PersonalGrade,
                rd.HRS_RdJobGrade AS JobGrade,
                rd.HRS_RdNote AS Note,
                rd.HRS_RdTitle AS Title,
                rd.HRS_RdSummaryJob AS SummaryJob,
                rt.HRS_RtRuleName AS RuleTypeName
            FROM dbo.HRS_RuleDocument rd
            LEFT JOIN dbo.HRS_RuleType rt ON rt.HRS_RtID = rd.HRS_RtID_fk
            WHERE rd.TBL_PersonnelID_fk IS NOT NULL
              AND rd.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if rd_df.empty:
            print("No rule documents found.")
            dest_cnxn.commit()
            return

        ensure_rank_codes_from_grades(
            dest_cnxn,
            dest_cursor,
            rd_df['PersonalGrade'].dropna().unique().tolist(),
        )

        emp_map_df = pd.read_sql("""
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """, dest_cnxn)
        merged = pd.merge(rd_df, emp_map_df, on='SourceID', how='left')
        skipped_no_employee = int(merged['EmployeeID'].isna().sum())
        work_df = merged[merged['EmployeeID'].notna()].copy()

        mapped_df = pd.read_sql("""
            SELECT SourceRuleDocumentID, DestEmployeeStatuteID
            FROM master.dbo.StatuteMigrationMapping
        """, dest_cnxn)
        already = {
            int(r['SourceRuleDocumentID']): int(r['DestEmployeeStatuteID'])
            for _, r in mapped_df.iterrows()
        }

        statute_last_id = ensure_table_id(dest_cursor, 'HCM3.EmployeeStatute', 0)
        insert_sql = """
            INSERT INTO HCM3.EmployeeStatute (
                EmployeeStatuteID, EmployeeRef, EmploymentTypeRef, StatuteTypeRef,
                Number, IssueDate, ApplyDate, ExpiryDate,
                OrganizationalStructureRef, PostRef, DepartmentRef, JobRef,
                WorkLocationCode, RankCode, Description, Status, Extra1Code,
                IssueTimeCode, AfterIssueStatusCode,
                IssueYearMonth, ApplyYearMonth,
                ConfirmDate, ConfirmYearMonth, PaymentConfirmDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                GETDATE(), 1, GETDATE(), 1
            )
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.StatuteMigrationMapping (
                SourceRuleDocumentID, DestEmployeeStatuteID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already = 0
        expiry_updated = 0
        job_updated = 0
        with_structure = 0
        without_structure = 0
        with_job = 0

        print(f"Inserting EmployeeStatute records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_rd_id = int(row['SourceRuleDocumentID'])

            employee_id = int(row['EmployeeID'])
            source_et = _positive_fk(row['SourceEmploymentTypeID'])
            source_place = _positive_fk(row['SourcePlaceID'])
            source_dept = _positive_fk(row['SourceDepartmentID'])
            source_post = _positive_fk(row['SourcePostID'])
            source_rt = _positive_fk(row['SourceRuleTypeID'])

            et_ref = et_map.get(source_et) if source_et else None
            place_code = place_map.get(source_place) if source_place else None
            dept_ref = dept_map.get(source_dept) if source_dept else None
            post_ref = post_map.get(source_post) if source_post else None
            statute_type_ref = statute_type_map.get(source_rt) if source_rt else None

            source_job = post_to_job.get(source_post) if source_post else None
            parent_job = job_map.get(source_job) if source_job else None
            job_ref = rank1_by_parent.get(parent_job) if parent_job else None
            if job_ref:
                with_job += 1

            number = clean_persian_text(row['RuleNo'])
            if number:
                number = number[:200]

            issue_date = _parse_shamsi_date(row['ExportDate'])
            apply_date = _parse_shamsi_date(row['ExecuteDate'])
            expiry_date = _parse_shamsi_date(row['EndDate'], treat_open_end_as_null=True)
            if expiry_date is None:
                # جاری: use تاریخ خاتمه قرارداد when EndDate empty/open
                expiry_date = _parse_shamsi_date(
                    row['ContractEndDate'], treat_open_end_as_null=True
                )

            rank_code = _job_class_rank(row['JobGrade'])
            extra1_code = _personal_grade_extra1(row['PersonalGrade'])
            issue_ym = gregorian_to_shamsi_year_month(issue_date)
            apply_ym = gregorian_to_shamsi_year_month(apply_date)
            issue_time_code, after_issue_status = type_codes.get(
                statute_type_ref, (None, None)
            )

            if source_rd_id in already:
                dest_statute_id = already[source_rd_id]
                dest_cursor.execute(
                    """
                    UPDATE HCM3.EmployeeStatute
                    SET ExpiryDate = ?,
                        JobRef = ?,
                        LastModificationDate = GETDATE(),
                        LastModifier = 1
                    WHERE EmployeeStatuteID = ?
                      AND (
                        (ExpiryDate IS NULL AND ? IS NOT NULL)
                        OR (ExpiryDate IS NOT NULL AND ? IS NULL)
                        OR (ExpiryDate IS NOT NULL AND ? IS NOT NULL AND ExpiryDate <> ?)
                        OR ISNULL(JobRef, -1) <> ISNULL(?, -1)
                      )
                    """,
                    (
                        expiry_date, job_ref, dest_statute_id,
                        expiry_date, expiry_date, expiry_date, expiry_date,
                        job_ref,
                    ),
                )
                if dest_cursor.rowcount:
                    # Count job fill separately when JobRef was previously null
                    if job_ref is not None:
                        job_updated += 1
                    else:
                        expiry_updated += 1
                skipped_already += 1
                continue

            structure_ref = _resolve_structure_ref(
                source_post,
                dept_ref,
                apply_date,
                post_oc_map,
                structure_map,
                structure_meta,
            )
            if structure_ref:
                with_structure += 1
            else:
                without_structure += 1

            desc_parts = []
            rule_title = clean_persian_text(row['Title'])
            rule_type_name = clean_persian_text(row['RuleTypeName'])
            note = clean_persian_text(row['Note'])
            summary = clean_persian_text(row['SummaryJob'])
            for part in (rule_type_name, rule_title, summary, note):
                if part:
                    desc_parts.append(part)
            description = ' | '.join(desc_parts)[:2000] if desc_parts else None

            statute_last_id += 1
            dest_cursor.execute(
                insert_sql,
                (
                    statute_last_id,
                    employee_id,
                    et_ref,
                    statute_type_ref,
                    number,
                    issue_date,
                    apply_date,
                    expiry_date,
                    structure_ref,
                    post_ref,
                    dept_ref,
                    job_ref,
                    place_code,
                    rank_code,
                    description,
                    STATUTE_STATUS_PAYMENT_CONFIRMED,
                    extra1_code,
                    issue_time_code,
                    after_issue_status,
                    issue_ym,
                    apply_ym,
                    issue_date,
                    issue_ym,
                    issue_date,
                ),
            )
            dest_cursor.execute(insert_mapping_sql, (source_rd_id, statute_last_id))
            already[source_rd_id] = statute_last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeStatute'",
            (statute_last_id,),
        )
        ensure_statute_type_profile(source_cnxn, dest_cnxn, dest_cursor)
        _apply_statute_rank_extra_dates_and_type_codes(source_cnxn, dest_cnxn, dest_cursor)
        _fill_employment_date_and_payment_status(source_cnxn, dest_cnxn, dest_cursor)

        dest_cnxn.commit()
        print(
            f"Success! Statutes inserted: {inserted}. "
            f"Skipped (already mapped): {skipped_already}. "
            f"ExpiryDate/JobRef updated: {expiry_updated + job_updated} "
            f"(job fills: {job_updated}). "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"With structure ref: {with_structure}. "
            f"Without structure ref: {without_structure}. "
            f"With JobRef (candidates): {with_job}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Employee Statute step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
