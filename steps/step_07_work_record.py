import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, ensure_lookup_codes
from utils.org_migration import (
    ensure_departments,
    ensure_jobs,
    ensure_posts,
    ensure_table_id,
)

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_ORG_NAME = '-'
OPEN_END_SHAMSI = '1499/12/29'
DEFAULT_DEGREE_CODE = 1

# WorkRelationType codes (SYS3.Lookup Type = WorkRelationType)
WORK_RELATION_RELATED = 1          # مرتبط
WORK_RELATION_GOV_IN_INDUSTRY = 3  # دولتی داخل صنعت
WORK_RELATION_GOV_OUT_INDUSTRY = 4  # دولتی خارج از صنعت
WORK_RELATION_PRIV_IN_INDUSTRY = 5  # خصوصی داخل صنعت
WORK_RELATION_PRIV_OUT_INDUSTRY = 6  # خصوصی خارج از صنعت

WORK_RELATION_LOOKUP_VALUES = {
    WORK_RELATION_GOV_IN_INDUSTRY: 'دولتی داخل صنعت',
    WORK_RELATION_GOV_OUT_INDUSTRY: 'دولتی خارج از صنعت',
    WORK_RELATION_PRIV_IN_INDUSTRY: 'خصوصی داخل صنعت',
    WORK_RELATION_PRIV_OUT_INDUSTRY: 'خصوصی خارج از صنعت',
}

# WorkRecordExtra1: active flag on EmployeeWorkRecord.Extra1Code
WORK_RECORD_EXTRA1_ACTIVE = 1      # فعال
WORK_RECORD_EXTRA1_INACTIVE = 2    # غیرفعال
WORK_RECORD_EXTRA1_LOOKUP_VALUES = {
    WORK_RECORD_EXTRA1_ACTIVE: 'فعال',
    WORK_RECORD_EXTRA1_INACTIVE: 'غیرفعال',
}

# WorkRecordExtra2: supervision/heading right from HRS_EshHeadingStatus
WORK_RECORD_EXTRA2_LOOKUP_VALUES = {
    0: 'بدون حق سرپرستی',
    1: 'دارای حق سرپرستی',
    2: 'دارای حق سرپرستی (نوع ۲)',
}

# Internal work type (دولتی داخل شرکت)
WORK_TYPE_INTERNAL = 1

# HRS_EshType → (WorkTypeCode, WorkRelationTypeCode)
ESH_TYPE_WORK_CODES = {
    1: (WORK_TYPE_INTERNAL, WORK_RELATION_RELATED),
    2: (2, WORK_RELATION_GOV_IN_INDUSTRY),
    3: (2, WORK_RELATION_GOV_OUT_INDUSTRY),
    4: (2, WORK_RELATION_PRIV_OUT_INDUSTRY),
    5: (2, WORK_RELATION_PRIV_IN_INDUSTRY),
}


def _parse_shamsi_date(raw, *, treat_open_end_as_null=False):
    """Parse Shamsi date strings; reject junk; optionally treat open-ended as NULL."""
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


def setup_work_record_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'WorkRecordMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.WorkRecordMigrationMapping (
                SourceEmploymentServiceHistoryID BIGINT PRIMARY KEY,
                DestEmployeeWorkRecordID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _work_type_and_relation(esh_type):
    try:
        t = int(esh_type)
    except (TypeError, ValueError):
        t = 1
    return ESH_TYPE_WORK_CODES.get(t, (2, WORK_RELATION_PRIV_OUT_INDUSTRY))


def _extra1_active_code(esh_active):
    try:
        return WORK_RECORD_EXTRA1_ACTIVE if int(esh_active) == 1 else WORK_RECORD_EXTRA1_INACTIVE
    except (TypeError, ValueError):
        return WORK_RECORD_EXTRA1_INACTIVE


def _extra2_heading_code(heading_status):
    try:
        code = int(heading_status)
    except (TypeError, ValueError):
        return 0
    if code in WORK_RECORD_EXTRA2_LOOKUP_VALUES:
        return code
    return 0


def _as_int_or_none(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _positive_fk(val):
    num = _as_int_or_none(val)
    return num if num is not None and num > 0 else None


def _positive_score(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _insurance_duration(insurance_raw, duration_raw):
    """Prefer source insurance duration when >0; otherwise fall back to Duration."""
    insurance = _as_int_or_none(insurance_raw)
    if insurance is not None and insurance > 0:
        return insurance
    return _as_int_or_none(duration_raw)


def _resolve_org_fks(row, dept_map, post_map):
    source_dept_id = _positive_fk(row.get('SourceDepartmentID'))
    source_post_id = _positive_fk(row.get('SourcePostID'))
    department_ref = dept_map.get(source_dept_id) if source_dept_id else None
    post_ref = post_map.get(source_post_id) if source_post_id else None
    return post_ref, department_ref


def run():
    print("\n--- Running Step 7: Employee Work Record Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_work_record_mapping_table(dest_cursor)

        print("Migrating Departments...")
        dept_map = ensure_departments(source_cnxn, dest_cnxn, dest_cursor)

        print("Migrating Posts...")
        post_map = ensure_posts(source_cnxn, dest_cnxn, dest_cursor)

        print("Fetching Source Employment Service History...")
        source_df = pd.read_sql("""
            SELECT
                esh.HRS_EshID AS SourceEmploymentServiceHistoryID,
                esh.TBL_PersonnelID_fk AS SourceID,
                esh.HRS_EshStartDate AS StartDate,
                esh.HRS_EshEndDate AS EndDate,
                esh.HRS_EshPostName AS PostName,
                esh.HRS_EshTime AS Duration,
                esh.HRS_EshInsuranceDuration AS InsuranceDuration,
                esh.HRS_EshScore AS Score,
                esh.HRS_EshNote AS Note,
                esh.HRS_EshType AS EshType,
                esh.HRS_EshActive AS EshActive,
                esh.HRS_EshHeadingStatus AS HeadingStatus,
                esh.HRS_JobRelationID_fk AS JobRelationID,
                esh.HRS_CompanyID_fk AS CompanyID,
                esh.TBL_DegreeID_fk AS SourceDegreeID,
                esh.TBL_DepartmentID_fk AS SourceDepartmentID,
                esh.TBL_PostID_fk AS SourcePostID,
                esh.TBL_JobID_fk AS SourceJobID,
                j.TBL_JobName AS JobName,
                d.TBL_DegreeName AS DegreeName
            FROM dbo.HRS_EmploymentServiceHistory esh
            LEFT JOIN dbo.TBL_Job j ON j.TBL_JobID = esh.TBL_JobID_fk
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = esh.TBL_DegreeID_fk
            WHERE esh.TBL_PersonnelID_fk IS NOT NULL
              AND esh.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No employment service history rows found.")
            dest_cnxn.commit()
            return

        print("Migrating Jobs referenced by ESH...")
        job_ids = [
            int(j) for j in source_df['SourceJobID'].dropna().unique()
            if _positive_fk(j)
        ]
        job_map = ensure_jobs(source_cnxn, dest_cnxn, dest_cursor, source_job_ids=job_ids)

        print("Fetching company titles...")
        company_df = pd.read_sql("""
            SELECT
                HRS_CompanyID_fk AS CompanyID,
                MAX(HRS_EshCompanyTitle) AS CompanyTitle
            FROM dbo.V_HRS_Esh_API
            WHERE HRS_CompanyID_fk IS NOT NULL
            GROUP BY HRS_CompanyID_fk
        """, source_cnxn)
        source_df = pd.merge(source_df, company_df, on='CompanyID', how='left')

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
            dest_cnxn.commit()
            return

        mapped_df = pd.read_sql("""
            SELECT SourceEmploymentServiceHistoryID, DestEmployeeWorkRecordID
            FROM master.dbo.WorkRecordMigrationMapping
        """, dest_cnxn)
        already_mapped = {}
        if not mapped_df.empty:
            already_mapped = {
                int(row['SourceEmploymentServiceHistoryID']): int(row['DestEmployeeWorkRecordID'])
                for _, row in mapped_df.iterrows()
            }

        print("Synchronizing degree mappings...")
        degree_id_map = ensure_degree_mappings(
            source_cnxn,
            dest_cnxn,
            dest_cursor,
            work_df[['SourceDegreeID', 'DegreeName']],
        )

        print("Ensuring WorkRelationType lookup values...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'WorkRelationType',
            WORK_RELATION_LOOKUP_VALUES,
        )

        print("Ensuring WorkRecordExtra1 lookup values...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'WorkRecordExtra1',
            WORK_RECORD_EXTRA1_LOOKUP_VALUES,
        )

        print("Ensuring WorkRecordExtra2 lookup values...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'WorkRecordExtra2',
            WORK_RECORD_EXTRA2_LOOKUP_VALUES,
        )

        print("Preparing ID generator...")
        work_last_id = ensure_table_id(dest_cursor, 'HCM3.EmployeeWorkRecord', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeWorkRecord (
                EmployeeWorkRecordID, EmployeeRef, WorkTypeCode, OrgName, Role,
                WorkRelationTypeCode, EducationDegreeCode, StartDate, EndDate,
                EffectiveDate, Duration, InsuranceDuration, Score, Description,
                PostRef, DepartmentRef, JobRef, Extra1Code, Extra2Code,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.WorkRecordMigrationMapping (
                SourceEmploymentServiceHistoryID, DestEmployeeWorkRecordID
            ) VALUES (?, ?)
        """
        update_mapped_sql = """
            UPDATE HCM3.EmployeeWorkRecord
            SET WorkTypeCode = ?,
                WorkRelationTypeCode = ?,
                Extra1Code = ?,
                Extra2Code = ?,
                InsuranceDuration = ?,
                JobRef = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeWorkRecordID = ?
        """
        inserted = 0
        types_corrected = 0
        skipped_already_mapped = 0
        open_ended_ends = 0
        defaulted_org = 0
        defaulted_degree = 0
        defaulted_effective = 0
        insurance_from_duration = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeWorkRecord records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceEmploymentServiceHistoryID'])
            work_type_code, work_relation_code = _work_type_and_relation(row['EshType'])
            extra1_code = _extra1_active_code(row['EshActive'])
            extra2_code = _extra2_heading_code(row['HeadingStatus'])

            duration = _as_int_or_none(row['Duration'])
            insurance_raw = _as_int_or_none(row['InsuranceDuration'])
            insurance_duration = _insurance_duration(row['InsuranceDuration'], row['Duration'])
            if (
                (insurance_raw is None or insurance_raw <= 0)
                and insurance_duration is not None
            ):
                insurance_from_duration += 1

            source_job_id = _positive_fk(row.get('SourceJobID'))
            job_ref = job_map.get(source_job_id) if source_job_id else None

            if source_id in already_mapped:
                dest_wr_id = already_mapped[source_id]
                dest_cursor.execute(
                    update_mapped_sql,
                    (
                        work_type_code,
                        work_relation_code,
                        extra1_code,
                        extra2_code,
                        insurance_duration,
                        job_ref,
                        dest_wr_id,
                    ),
                )
                types_corrected += 1
                skipped_already_mapped += 1
                continue

            post_ref, department_ref = _resolve_org_fks(row, dept_map, post_map)
            employee_id = int(row['EmployeeID'])

            org_name = clean_persian_text(row['CompanyTitle'])
            if org_name is None:
                org_name = DEFAULT_ORG_NAME
                defaulted_org += 1
            org_name = org_name[:400]

            role = clean_persian_text(row['JobName'])
            if role is None:
                role = clean_persian_text(row['PostName'])
            if role is not None:
                role = role[:200]

            start_date = _parse_shamsi_date(row['StartDate'])
            end_raw = row['EndDate']
            end_text = (
                str(end_raw).strip().split()[0]
                if end_raw is not None and not (isinstance(end_raw, float) and pd.isna(end_raw))
                else ''
            )
            if end_text == OPEN_END_SHAMSI:
                end_date = None
                open_ended_ends += 1
            else:
                end_date = _parse_shamsi_date(end_raw, treat_open_end_as_null=True)

            effective_date = start_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            score = _positive_score(row['Score'])
            description = clean_persian_text(row['Note'])

            degree_code = DEFAULT_DEGREE_CODE
            try:
                source_degree_id = int(row['SourceDegreeID'])
            except (TypeError, ValueError):
                source_degree_id = 0
            if source_degree_id > 0 and source_degree_id in degree_id_map:
                degree_code = int(degree_id_map[source_degree_id])
            else:
                defaulted_degree += 1

            work_last_id += 1
            dest_cursor.execute(insert_sql, (
                work_last_id,
                employee_id,
                work_type_code,
                org_name,
                role,
                work_relation_code,
                degree_code,
                start_date,
                end_date,
                effective_date,
                duration,
                insurance_duration,
                score,
                description,
                post_ref,
                department_ref,
                job_ref,
                extra1_code,
                extra2_code,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_id, work_last_id))
            already_mapped[source_id] = work_last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeWorkRecord'",
            (work_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Work records inserted: {inserted}. "
            f"Types/insurance/job corrected: {types_corrected}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Open-ended ends: {open_ended_ends}. "
            f"Insurance from duration: {insurance_from_duration}. "
            f"Defaulted org: {defaulted_org}. "
            f"Defaulted degree: {defaulted_degree}. "
            f"Defaulted effective date: {defaulted_effective}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Work Record step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
