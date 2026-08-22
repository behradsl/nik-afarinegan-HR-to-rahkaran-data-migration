"""Step 11: Migrate HRS_RuleDocument → HCM3.EmployeeStatute."""
import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian
from utils.date_helpers import shamsi_to_gregorian
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
)

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'
# EmployeeStatute.Status — active/confirmed analogue
STATUTE_STATUS_ACTIVE = 1

# StatuteType required defaults (Rahkaran NOT NULL columns)
STATUTE_TYPE_DEFAULTS = {
    'IssueTimeCode': 1,
    'AfterIssueStatusCode': 1,
    'PostSelectionTypeCode': 2,       # انتخاب از فهرست پستها
    'TempPostSelectionTypeCode': 1,
    'Status': 1,
    'RelatedStatuteTypeCode': 1,
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
            HRS_RtActive AS RuleActive
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
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
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
                WorkLocationCode, RankCode, Description, Status,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
            job_ref = job_map.get(source_job) if source_job else None
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

            rank_code = _positive_fk(row['PersonalGrade'])

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
                    STATUTE_STATUS_ACTIVE,
                ),
            )
            dest_cursor.execute(insert_mapping_sql, (source_rd_id, statute_last_id))
            already[source_rd_id] = statute_last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeStatute'",
            (statute_last_id,),
        )

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
