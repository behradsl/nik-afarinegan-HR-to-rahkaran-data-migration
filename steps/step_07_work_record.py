import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_value, normalize_persian
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_ORG_NAME = '-'
DEFAULT_TITLE = '-'
OPEN_END_SHAMSI = '1499/12/29'
DEFAULT_DEGREE_CODE = 1


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


def setup_department_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'DepartmentMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.DepartmentMigrationMapping (
                SourceDepartmentID BIGINT PRIMARY KEY,
                DestDepartmentID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_post_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'PostMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.PostMigrationMapping (
                SourcePostID BIGINT PRIMARY KEY,
                DestPostID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
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


def _work_type_code(esh_type):
    try:
        t = int(esh_type)
    except (TypeError, ValueError):
        return 1
    if t in (3, 4):
        return 2  # خارجی
    return 1  # داخلی (1,2,5 and unknown)


def _work_relation_code(job_relation_id):
    try:
        rid = int(job_relation_id)
    except (TypeError, ValueError):
        return 2
    return 1 if rid == 300001 else 2


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
    num = None
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _title_and_code(title_raw, code_raw, source_id, title_max=200, code_max=100):
    title = clean_value(title_raw)
    if title is not None:
        title = normalize_persian(str(title).strip()) or None
    if not title:
        title = DEFAULT_TITLE
    title = title[:title_max]

    code = clean_value(code_raw)
    if code is not None:
        code = str(code).strip() or None
    if not code or code == '0':
        code = str(source_id)
    code = code[:code_max]
    return title, code


def _unique_code_for_title(base_code, title, source_id, used_pairs, code_max=100):
    """Ensure (Code, Title) is unique for HCM3.Department UIX_HCM3_Department_Code_Title."""
    code = base_code[:code_max]
    if (code, title) not in used_pairs:
        return code
    suffix = f"-{source_id}"
    code = f"{base_code[:max(0, code_max - len(suffix))]}{suffix}"
    if (code, title) not in used_pairs:
        return code
    # Last resort: source id alone
    return str(source_id)[:code_max]


def _active_status(active_raw):
    try:
        return 1 if int(float(active_raw)) == 1 else 2
    except (TypeError, ValueError):
        return 2


def ensure_departments(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_Department -> HCM3.Department. Returns SourceDepartmentID -> DestDepartmentID."""
    setup_department_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_DepartmentID AS SourceDepartmentID,
            TBL_DepartmentName AS DepartmentName,
            TBL_DepartmentCode AS DepartmentCode,
            TBL_DepartmentActive AS DepartmentActive
        FROM dbo.TBL_Department
        WHERE TBL_DepartmentID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourceDepartmentID, DestDepartmentID FROM master.dbo.DepartmentMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourceDepartmentID']): int(row['DestDepartmentID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source departments found.")
        return result

    missing_df = source_df[~source_df['SourceDepartmentID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Departments already mapped: {len(result)}.")
        return result

    existing_pairs_df = pd.read_sql(
        "SELECT Code, Title FROM HCM3.Department WHERE Code IS NOT NULL",
        dest_cnxn,
    )
    used_pairs = {
        (str(row['Code']), normalize_persian(str(row['Title'])) if row['Title'] else DEFAULT_TITLE)
        for _, row in existing_pairs_df.iterrows()
    }

    last_id = _ensure_table_id(dest_cursor, 'HCM3.Department', 0)
    insert_sql = """
        INSERT INTO HCM3.Department (
            DepartmentID, Code, Title, RegionalDivisionRef, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, NULL, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.DepartmentMigrationMapping (
            SourceDepartmentID, DestDepartmentID
        ) VALUES (?, ?)
    """

    inserted = 0
    uniquified = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourceDepartmentID'])
        title, base_code = _title_and_code(
            row['DepartmentName'], row['DepartmentCode'], source_id, title_max=200
        )
        code = _unique_code_for_title(base_code, title, source_id, used_pairs)
        if code != base_code:
            uniquified += 1
        status = _active_status(row['DepartmentActive'])
        last_id += 1
        dest_cursor.execute(insert_sql, (last_id, code, title, status))
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        used_pairs.add((code, title))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Department'",
        (last_id,),
    )
    print(
        f"  -> Departments inserted: {inserted} "
        f"(codes uniquified: {uniquified}). Total mapped: {len(result)}."
    )
    return result


def ensure_posts(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_Post -> HCM3.Post. Returns SourcePostID -> DestPostID."""
    setup_post_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_PostID AS SourcePostID,
            TBL_PostTitle AS PostTitle,
            TBL_PostCode AS PostCode,
            TBL_PostActive AS PostActive
        FROM dbo.TBL_Post
        WHERE TBL_PostID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourcePostID, DestPostID FROM master.dbo.PostMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourcePostID']): int(row['DestPostID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source posts found.")
        return result

    missing_df = source_df[~source_df['SourcePostID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Posts already mapped: {len(result)}.")
        return result

    last_id = _ensure_table_id(dest_cursor, 'HCM3.Post', 0)
    insert_sql = """
        INSERT INTO HCM3.Post (
            PostID, Code, Title, TypeCode, RegionalDivisionRef, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, NULL, NULL, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.PostMigrationMapping (
            SourcePostID, DestPostID
        ) VALUES (?, ?)
    """

    inserted = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourcePostID'])
        title, code = _title_and_code(
            row['PostTitle'], row['PostCode'], source_id, title_max=400
        )
        status = _active_status(row['PostActive'])
        last_id += 1
        dest_cursor.execute(insert_sql, (last_id, code, title, status))
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Post'",
        (last_id,),
    )
    print(f"  -> Posts inserted: {inserted}. Total mapped: {len(result)}.")
    return result


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
                esh.HRS_JobRelationID_fk AS JobRelationID,
                esh.HRS_CompanyID_fk AS CompanyID,
                esh.TBL_DegreeID_fk AS SourceDegreeID,
                esh.TBL_DepartmentID_fk AS SourceDepartmentID,
                esh.TBL_PostID_fk AS SourcePostID,
                j.TBL_JobName AS JobName,
                d.TBL_DegreeName AS DegreeName
            FROM dbo.HRS_EmploymentServiceHistory esh
            LEFT JOIN dbo.TBL_Job j ON j.TBL_JobID = esh.TBL_JobID_fk
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = esh.TBL_DegreeID_fk
            WHERE esh.HRS_EshActive = 1
              AND esh.TBL_PersonnelID_fk IS NOT NULL
              AND esh.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No active employment service history rows found.")
            dest_cnxn.commit()
            return

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

        print("Preparing ID generator...")
        work_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeWorkRecord', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeWorkRecord (
                EmployeeWorkRecordID, EmployeeRef, WorkTypeCode, OrgName, Role,
                WorkRelationTypeCode, EducationDegreeCode, StartDate, EndDate,
                EffectiveDate, Duration, InsuranceDuration, Score, Description,
                PostRef, DepartmentRef,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.WorkRecordMigrationMapping (
                SourceEmploymentServiceHistoryID, DestEmployeeWorkRecordID
            ) VALUES (?, ?)
        """
        inserted = 0
        skipped_already_mapped = 0
        open_ended_ends = 0
        defaulted_org = 0
        defaulted_degree = 0
        defaulted_effective = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeWorkRecord records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceEmploymentServiceHistoryID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            post_ref, department_ref = _resolve_org_fks(row, dept_map, post_map)
            employee_id = int(row['EmployeeID'])

            org_name = clean_value(row['CompanyTitle'])
            if org_name is None:
                org_name = DEFAULT_ORG_NAME
                defaulted_org += 1
            else:
                org_name = normalize_persian(str(org_name).strip()) or DEFAULT_ORG_NAME
                if org_name == DEFAULT_ORG_NAME:
                    defaulted_org += 1
            org_name = org_name[:400]

            role = clean_value(row['JobName'])
            if role is not None:
                role = normalize_persian(str(role).strip()) or None
            if role is None:
                post_name = clean_value(row['PostName'])
                if post_name is not None:
                    role = normalize_persian(str(post_name).strip()) or None
            if role is not None:
                role = role[:200]

            start_date = _parse_shamsi_date(row['StartDate'])
            end_raw = row['EndDate']
            end_text = str(end_raw).strip().split()[0] if end_raw is not None and not (isinstance(end_raw, float) and pd.isna(end_raw)) else ''
            if end_text == OPEN_END_SHAMSI:
                end_date = None
                open_ended_ends += 1
            else:
                end_date = _parse_shamsi_date(end_raw, treat_open_end_as_null=True)

            effective_date = start_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            duration = _as_int_or_none(row['Duration'])
            insurance_duration = _as_int_or_none(row['InsuranceDuration'])
            score = _positive_score(row['Score'])

            description = clean_value(row['Note'])
            if description is not None:
                description = normalize_persian(str(description).strip()) or None

            work_type_code = _work_type_code(row['EshType'])
            work_relation_code = _work_relation_code(row['JobRelationID'])

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
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Open-ended ends: {open_ended_ends}. "
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
