"""
Step 15: Migrate HRS_EvaluationPersonnel → HCM3.EmployeeAppraisal (Tier 1 only).

- Sync assessment types into AppraisalType → TypeCode
- Result = source score
- Year / type / extras go in Title and Description only
- No PerformancePeriod / Process creation
"""
import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import sync_lookup

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_TITLE = '-'
PERFORMANCE_PERIOD_TYPE_NON_SYSTEM = 2


def _parse_shamsi_date(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    date_part = text.split()[0]
    if date_part in ('', '0', '____/__/__', '/  /', '//', '0/0/0'):
        return None
    if '_' in date_part or date_part.count('/') != 2:
        return None
    return shamsi_to_gregorian(date_part)


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


def setup_appraisal_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'AppraisalMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.AppraisalMigrationMapping (
                SourceEvaluationPersonnelID BIGINT PRIMARY KEY,
                DestEmployeeAppraisalID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _score_result(val):
    """Result is nvarchar NOT NULL; use source score text (including 0)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return '0'
    try:
        num = float(val)
        if num == int(num):
            return str(int(num))
        return str(num)
    except (TypeError, ValueError):
        text = str(val).strip()
        return text if text else '0'


def _valid_year(val):
    try:
        year = int(val)
    except (TypeError, ValueError):
        return None
    if 1370 <= year <= 1410:
        return year
    return None


def _fmt_num(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num == 0:
        return None
    if num == int(num):
        return str(int(num))
    return str(num)


def _build_title(type_name, year):
    parts = []
    if type_name and type_name != DEFAULT_TITLE:
        parts.append(type_name)
    if year:
        parts.append(str(year))
    title = ' '.join(parts) if parts else DEFAULT_TITLE
    return title[:400]


def _build_description(row, year):
    parts = []
    if year:
        parts.append(f'سال: {year}')
    type_name = row.get('AssessmentTypeClean')
    if type_name and type_name != DEFAULT_TITLE:
        parts.append(f'نوع: {type_name}')

    for label, col in (
        ('امتیاز', 'EpScore'),
        ('نمره', 'EpMark'),
        ('رتبه', 'EpGrade'),
        ('عوامل اختصاصی', 'SpecificScore'),
        ('عوامل عمومی', 'GeneralScore'),
        ('توسعه', 'DevelopmentScore'),
        ('تشویق', 'EncouragementScore'),
        ('رفتاری', 'BehavioralScore'),
        ('حاکمیتی', 'GovernorScore'),
    ):
        text = _fmt_num(row.get(col))
        if text is not None:
            parts.append(f'{label}: {text}')

    note = clean_persian_text(row.get('EpNote'))
    if note:
        parts.append(note)
    return '\n'.join(parts) if parts else None


def run():
    print("\n--- Running Step 15: Evaluation → EmployeeAppraisal Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_appraisal_mapping_table(dest_cursor)

        print("Fetching Source Evaluation Personnel...")
        source_df = pd.read_sql("""
            SELECT
                ep.HRS_EpID AS SourceEvaluationPersonnelID,
                ep.TBL_PersonnelID_fk AS SourceID,
                ep.HRS_EpDate AS EpDate,
                ep.HRS_EpYear AS EpYear,
                ep.HRS_EpScore AS EpScore,
                ep.HRS_EpMark AS EpMark,
                ep.HRS_EpGrade AS EpGrade,
                ep.HRS_EpNote AS EpNote,
                ep.HRS_EpSpecificFactorsScore AS SpecificScore,
                ep.HRS_EpGeneralFactorsScore AS GeneralScore,
                ep.HRS_EpProcessDevelopmentScore AS DevelopmentScore,
                ep.HRS_EpProcessEncouragmentScore AS EncouragementScore,
                ep.HRS_EpProcessBehavioralScore AS BehavioralScore,
                ep.HRS_EpGovernorScore AS GovernorScore,
                pb.HRS_PayBaseName AS AssessmentTypeName
            FROM dbo.HRS_EvaluationPersonnel ep
            LEFT JOIN dbo.HRS_PayBase pb
                ON pb.HRS_PayBaseID = ep.HRS_AssessmentTypeID_fk
            WHERE ep.HRS_EpID > 0
              AND ep.TBL_PersonnelID_fk IS NOT NULL
              AND ep.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No evaluation personnel rows found.")
            return

        print("Mapping Source to Rahkaran Employees...")
        emp_map_df = pd.read_sql("""
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """, dest_cnxn)

        merged_df = pd.merge(source_df, emp_map_df, on='SourceID', how='left')
        skipped_no_employee = int(merged_df['EmployeeID'].isna().sum())
        work_df = merged_df[merged_df['EmployeeID'].notna()].copy()

        if work_df.empty:
            print(f"No matching employees found. Skipped (no employee): {skipped_no_employee}.")
            return

        work_df['AssessmentTypeClean'] = work_df['AssessmentTypeName'].apply(clean_persian_text)
        skipped_no_type = int(work_df['AssessmentTypeClean'].isna().sum())
        work_df.loc[work_df['AssessmentTypeClean'].isna(), 'AssessmentTypeClean'] = DEFAULT_TITLE

        print("Syncing AppraisalType lookups from assessment types...")
        type_names = [
            n for n in work_df['AssessmentTypeClean'].unique()
            if n and n != DEFAULT_TITLE
        ]
        name_to_code = sync_lookup(dest_cnxn, dest_cursor, 'AppraisalType', type_names)
        if not name_to_code:
            name_to_code = {DEFAULT_TITLE: 1}
        default_type_code = int(next(iter(name_to_code.values())))

        mapped_df = pd.read_sql(
            "SELECT SourceEvaluationPersonnelID FROM master.dbo.AppraisalMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourceEvaluationPersonnelID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Preparing ID generator...")
        last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeAppraisal', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeAppraisal (
                EmployeeAppraisalID, EmployeeRef, Title, TypeCode, Result,
                EffectiveDate, Description, PerformancePeriodType, IsAutomated,
                PerformanceManagementProcessRef, PerformancePeriodRef,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.AppraisalMigrationMapping (
                SourceEvaluationPersonnelID, DestEmployeeAppraisalID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        defaulted_effective = 0
        skipped_no_type_code = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeAppraisal records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceEvaluationPersonnelID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            type_name = row['AssessmentTypeClean']
            type_code = name_to_code.get(type_name)
            if type_code is None:
                type_code = default_type_code
                skipped_no_type_code += 1
            type_code = int(type_code)

            year = _valid_year(row['EpYear'])
            title = _build_title(type_name, year)
            description = _build_description(row, year)

            effective_date = _parse_shamsi_date(row['EpDate'])
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            result = _score_result(row['EpScore'])

            last_id += 1
            dest_cursor.execute(insert_sql, (
                last_id,
                int(row['EmployeeID']),
                title,
                type_code,
                result,
                effective_date,
                description,
                PERFORMANCE_PERIOD_TYPE_NON_SYSTEM,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
            already_mapped.add(source_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeAppraisal'",
            (last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! EmployeeAppraisal inserted: {inserted}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Rows without source type name: {skipped_no_type}. "
            f"Fallback type used: {skipped_no_type_code}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Defaulted effective date: {defaulted_effective}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during EmployeeAppraisal step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
