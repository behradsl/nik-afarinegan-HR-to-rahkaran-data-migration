"""
Step 19: Migrate HRS_ServiceLeakage (انقطاع از خدمت) → HCM3.EmployeeWorkRecord.

- WorkTypeCode = 1 (دولتی داخل شرکت) — all internal
- WorkRelationTypeCode = انقطاع از خدمت
- ResignationReasonCode = PayBase leakage title (parent 12)
- Post/Dept/ET/location/rank/statute fields from the statute in force at StartDate
"""
import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, ensure_lookup_codes, sync_lookup
from utils.hcm_extra_settings import ensure_hcm_extra_fields
from utils.org_migration import ensure_table_id

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'
DEFAULT_ORG_NAME = 'شرکت برق منطقه ای غرب'
DEFAULT_DEGREE_CODE = 1
WORK_TYPE_INTERNAL = 1

WORK_RELATION_SERVICE_INTERRUPTION = 7
WORK_RELATION_LOOKUP_VALUES = {
    WORK_RELATION_SERVICE_INTERRUPTION: 'انقطاع از خدمت',
}

WORK_RECORD_EXTRA1_ACTIVE = 1
WORK_RECORD_EXTRA1_INACTIVE = 2
WORK_RECORD_EXTRA1_LOOKUP_VALUES = {
    WORK_RECORD_EXTRA1_ACTIVE: 'فعال',
    WORK_RECORD_EXTRA1_INACTIVE: 'غیرفعال',
}


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


def _date_str(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return str(val)[:10]


def setup_service_leakage_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'ServiceLeakageMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.ServiceLeakageMigrationMapping (
                SourceServiceLeakageID BIGINT PRIMARY KEY,
                DestEmployeeWorkRecordID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _extra1_active_code(active):
    try:
        return WORK_RECORD_EXTRA1_ACTIVE if int(active) == 1 else WORK_RECORD_EXTRA1_INACTIVE
    except (TypeError, ValueError):
        return WORK_RECORD_EXTRA1_INACTIVE


def _load_statutes_by_employee(dest_cnxn):
    st_df = pd.read_sql("""
        SELECT
            s.EmployeeStatuteID,
            s.EmployeeRef,
            s.PostRef,
            s.DepartmentRef,
            s.EmploymentTypeRef,
            s.WorkLocationCode,
            s.RankCode,
            s.BaseCode,
            s.Number,
            s.ApplyDate,
            s.IssueDate,
            s.ExpiryDate
        FROM HCM3.EmployeeStatute s
        INNER JOIN master.dbo.StatuteMigrationMapping m
            ON m.DestEmployeeStatuteID = s.EmployeeStatuteID
        WHERE s.ApplyDate IS NOT NULL
    """, dest_cnxn)
    if st_df.empty:
        return {}

    for col in ('ApplyDate', 'IssueDate', 'ExpiryDate'):
        st_df[col] = st_df[col].apply(_date_str)

    by_emp = {}
    for emp_id, group in st_df.groupby('EmployeeRef'):
        by_emp[int(emp_id)] = group.reset_index(drop=True)
    return by_emp


def _best_statute(statutes_df, start_date):
    """
    Statute snapshot for leakage start:
    1) In force at start (ApplyDate <= start, not expired) — latest ApplyDate
    2) Else latest with ApplyDate <= start
    3) Else nearest ApplyDate to start
    """
    if statutes_df is None or statutes_df.empty or not start_date:
        return None

    start = _date_str(start_date)
    dated = statutes_df[statutes_df['ApplyDate'].notna()].copy()
    if dated.empty:
        return None

    before = dated[dated['ApplyDate'] <= start]
    if not before.empty:
        active = before[
            before['ExpiryDate'].isna() | (before['ExpiryDate'] >= start)
        ]
        pool = active if not active.empty else before
        pool = pool.sort_values(
            by=['ApplyDate', 'IssueDate', 'EmployeeStatuteID'],
            ascending=[False, False, False],
            na_position='last',
        )
        return pool.iloc[0]

    # No statute before start — take earliest after start
    after = dated[dated['ApplyDate'] > start].sort_values(
        by=['ApplyDate', 'IssueDate', 'EmployeeStatuteID'],
        ascending=[True, False, False],
        na_position='last',
    )
    if after.empty:
        return None
    return after.iloc[0]


def _load_post_titles(dest_cnxn):
    post_df = pd.read_sql("SELECT PostID, Title FROM HCM3.Post", dest_cnxn)
    if post_df.empty:
        return {}
    return {
        int(r['PostID']): clean_persian_text(r['Title'])
        for _, r in post_df.iterrows()
        if r['Title'] is not None and not (isinstance(r['Title'], float) and pd.isna(r['Title']))
    }


def _load_org_name_by_employee(dest_cnxn):
    """Prefer existing internal work-record OrgName per employee."""
    org_df = pd.read_sql("""
        SELECT EmployeeRef, OrgName
        FROM (
            SELECT
                EmployeeRef,
                OrgName,
                ROW_NUMBER() OVER (
                    PARTITION BY EmployeeRef
                    ORDER BY CASE WHEN EndDate IS NULL THEN 1 ELSE 0 END DESC,
                             StartDate DESC,
                             EmployeeWorkRecordID DESC
                ) AS rn
            FROM HCM3.EmployeeWorkRecord
            WHERE WorkTypeCode = ?
              AND OrgName IS NOT NULL
              AND LTRIM(RTRIM(OrgName)) <> N''
              AND OrgName <> N'-'
        ) x
        WHERE rn = 1
    """, dest_cnxn, params=[WORK_TYPE_INTERNAL])
    if org_df.empty:
        return {}
    return {
        int(r['EmployeeRef']): clean_persian_text(r['OrgName']) or DEFAULT_ORG_NAME
        for _, r in org_df.iterrows()
    }


def run():
    print("\n--- Running Step 19: Service Leakage → Work Record ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_service_leakage_mapping_table(dest_cursor)

        print("Ensuring WorkRelationType (انقطاع از خدمت)...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'WorkRelationType',
            WORK_RELATION_LOOKUP_VALUES,
            overwrite_values=True,
        )

        print("Ensuring WorkRecordExtra1 lookup values...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'WorkRecordExtra1',
            WORK_RECORD_EXTRA1_LOOKUP_VALUES,
        )
        ensure_hcm_extra_fields(dest_cursor, ('WorkRecordExtra1',))

        print("Fetching Service Leakage rows...")
        source_df = pd.read_sql("""
            SELECT
                sl.HRS_SlID AS SourceServiceLeakageID,
                sl.TBL_PersonnelID_fk AS SourceID,
                sl.HRS_PbID_fk AS LeakagePayBaseID,
                pb.HRS_PayBaseName AS LeakageTitle,
                sl.TBL_DegreeID_fk AS SourceDegreeID,
                d.TBL_DegreeName AS DegreeName,
                sl.HRS_SlStartDate AS StartDate,
                sl.HRS_SlEndDate AS EndDate,
                sl.HRS_SlTime AS Duration,
                sl.HRS_SlDescription AS Description,
                sl.HRS_SlNote AS Note,
                sl.HRS_SlActive AS SlActive
            FROM dbo.HRS_ServiceLeakage sl
            LEFT JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID = sl.HRS_PbID_fk
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = sl.TBL_DegreeID_fk
            WHERE sl.TBL_PersonnelID_fk IS NOT NULL
              AND sl.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No service leakage rows found.")
            dest_cnxn.commit()
            return

        print("Syncing ResignationReason from leakage titles...")
        titles = []
        for raw in source_df['LeakageTitle'].dropna().unique():
            title = clean_persian_text(raw)
            if title:
                titles.append(title)
        # Also ensure full parent-12 catalog so unused types exist if needed later
        catalog_df = pd.read_sql("""
            SELECT HRS_PayBaseName
            FROM dbo.HRS_PayBase
            WHERE HRS_PayBaseParentID_fk = 12
        """, source_cnxn)
        for raw in catalog_df['HRS_PayBaseName'].dropna().unique():
            title = clean_persian_text(raw)
            if title and title not in titles:
                titles.append(title)

        resignation_name_to_code = sync_lookup(
            dest_cnxn, dest_cursor, 'ResignationReason', titles
        )

        print("Synchronizing degree mappings...")
        degree_id_map = ensure_degree_mappings(
            source_cnxn,
            dest_cnxn,
            dest_cursor,
            source_df[['SourceDegreeID', 'DegreeName']],
        )

        print("Mapping personnel → employees...")
        emp_map_df = pd.read_sql("""
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """, dest_cnxn)
        merged = pd.merge(source_df, emp_map_df, on='SourceID', how='left')
        skipped_no_employee = int(merged['EmployeeID'].isna().sum())
        work_df = merged[merged['EmployeeID'].notna()].copy()

        if work_df.empty:
            print(f"No matching employees. Skipped (no employee): {skipped_no_employee}.")
            dest_cnxn.commit()
            return

        mapped_df = pd.read_sql("""
            SELECT SourceServiceLeakageID, DestEmployeeWorkRecordID
            FROM master.dbo.ServiceLeakageMigrationMapping
        """, dest_cnxn)
        already_mapped = {}
        if not mapped_df.empty:
            already_mapped = {
                int(r['SourceServiceLeakageID']): int(r['DestEmployeeWorkRecordID'])
                for _, r in mapped_df.iterrows()
            }

        print("Loading statutes / post titles / org names...")
        statutes_by_emp = _load_statutes_by_employee(dest_cnxn)
        post_titles = _load_post_titles(dest_cnxn)
        org_by_emp = _load_org_name_by_employee(dest_cnxn)

        work_last_id = ensure_table_id(dest_cursor, 'HCM3.EmployeeWorkRecord', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeWorkRecord (
                EmployeeWorkRecordID, EmployeeRef, WorkTypeCode, OrgName, Role,
                WorkRelationTypeCode, EducationDegreeCode, StartDate, EndDate,
                EffectiveDate, Duration, ResignationReasonCode, Description,
                PostRef, DepartmentRef, EmploymentTypeRef, WorkLocationCode,
                RankCode, BaseCode, StatuteNumber, StatuteApplyDate,
                Extra1Code,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                GETDATE(), 1, GETDATE(), 1
            )
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.ServiceLeakageMigrationMapping (
                SourceServiceLeakageID, DestEmployeeWorkRecordID
            ) VALUES (?, ?)
        """
        update_sql = """
            UPDATE HCM3.EmployeeWorkRecord
            SET WorkTypeCode = ?,
                OrgName = ?,
                Role = ?,
                WorkRelationTypeCode = ?,
                EducationDegreeCode = ?,
                StartDate = ?,
                EndDate = ?,
                EffectiveDate = ?,
                Duration = ?,
                ResignationReasonCode = ?,
                Description = ?,
                PostRef = ?,
                DepartmentRef = ?,
                EmploymentTypeRef = ?,
                WorkLocationCode = ?,
                RankCode = ?,
                BaseCode = ?,
                StatuteNumber = ?,
                StatuteApplyDate = ?,
                Extra1Code = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeWorkRecordID = ?
        """

        inserted = 0
        updated = 0
        with_statute = 0
        without_statute = 0
        defaulted_degree = 0
        defaulted_effective = 0
        defaulted_resignation = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting/updating leakage work records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceServiceLeakageID'])
            employee_id = int(row['EmployeeID'])

            start_date = _parse_shamsi_date(row['StartDate'])
            end_raw = row['EndDate']
            end_text = (
                str(end_raw).strip().split()[0]
                if end_raw is not None and not (isinstance(end_raw, float) and pd.isna(end_raw))
                else ''
            )
            if end_text == OPEN_END_SHAMSI:
                end_date = None
            else:
                end_date = _parse_shamsi_date(end_raw, treat_open_end_as_null=True)

            effective_date = start_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            duration = _as_int_or_none(row['Duration'])
            if duration is not None and duration <= 0:
                duration = None

            leakage_title = clean_persian_text(row['LeakageTitle'])
            resignation_code = (
                resignation_name_to_code.get(normalize_persian(leakage_title))
                if leakage_title
                else None
            )
            if resignation_code is None:
                # fallback: first catalog title or leave null
                defaulted_resignation += 1

            desc_parts = []
            for part in (
                clean_persian_text(row['Description']),
                clean_persian_text(row['Note']),
            ):
                if part:
                    desc_parts.append(part)
            description = ' | '.join(desc_parts) if desc_parts else leakage_title

            degree_code = DEFAULT_DEGREE_CODE
            source_degree_id = _positive_fk(row['SourceDegreeID'])
            if source_degree_id and source_degree_id in degree_id_map:
                degree_code = int(degree_id_map[source_degree_id])
            else:
                defaulted_degree += 1

            statute = _best_statute(statutes_by_emp.get(employee_id), start_date)
            post_ref = None
            department_ref = None
            et_ref = None
            location_code = None
            rank_code = None
            base_code = None
            statute_number = None
            statute_apply = None
            role = None

            if statute is not None:
                with_statute += 1
                post_ref = _as_int_or_none(statute['PostRef'])
                department_ref = _as_int_or_none(statute['DepartmentRef'])
                et_ref = _as_int_or_none(statute['EmploymentTypeRef'])
                location_code = _as_int_or_none(statute['WorkLocationCode'])
                rank_code = _as_int_or_none(statute['RankCode'])
                base_code = _as_int_or_none(statute['BaseCode'])
                statute_apply = statute['ApplyDate']
                number = statute['Number']
                if number is not None and not (isinstance(number, float) and pd.isna(number)):
                    statute_number = str(number)[:200]
                if post_ref and post_ref in post_titles:
                    role = post_titles[post_ref]
                    if role:
                        role = role[:200]
            else:
                without_statute += 1

            if role is None and leakage_title:
                role = leakage_title[:200]

            org_name = org_by_emp.get(employee_id) or DEFAULT_ORG_NAME
            org_name = org_name[:400]
            extra1_code = _extra1_active_code(row['SlActive'])

            values_core = (
                WORK_TYPE_INTERNAL,
                org_name,
                role,
                WORK_RELATION_SERVICE_INTERRUPTION,
                degree_code,
                start_date,
                end_date,
                effective_date,
                duration,
                resignation_code,
                description,
                post_ref,
                department_ref,
                et_ref,
                location_code,
                rank_code,
                base_code,
                statute_number,
                statute_apply,
                extra1_code,
            )

            if source_id in already_mapped:
                dest_wr_id = already_mapped[source_id]
                dest_cursor.execute(update_sql, values_core + (dest_wr_id,))
                updated += 1
                continue

            work_last_id += 1
            dest_cursor.execute(insert_sql, (work_last_id, employee_id) + values_core)
            dest_cursor.execute(insert_mapping_sql, (source_id, work_last_id))
            already_mapped[source_id] = work_last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeWorkRecord'",
            (work_last_id,),
        )
        dest_cnxn.commit()
        print(
            f"Success! Leakage work records inserted: {inserted}. "
            f"Updated: {updated}. "
            f"With statute snapshot: {with_statute}. "
            f"Without statute: {without_statute}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Defaulted degree: {defaulted_degree}. "
            f"Defaulted effective: {defaulted_effective}. "
            f"Missing resignation title: {defaulted_resignation}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Service Leakage work-record step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
