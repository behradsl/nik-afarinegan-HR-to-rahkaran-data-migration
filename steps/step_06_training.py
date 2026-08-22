"""
Step 6: Migrate LMS_EducationHistory → HCM3.EmployeeTraining.

CourseSubjectCode = LMS_EhCertificateType_fk → LMS_LmsBase (نوع گواهینامه)
                  → SYS3.Lookup CourseSubject

Extra1Code = LMS_EhTakeScore (تعلق امتیاز)
           → SYS3.Lookup EmployeeTrainingExtra1

Extra2Code = EducationHistoryStatus categories from LMS_Eh_GetData_API
             TypeOperation=104 (دوره های گذرانده / عملکرد آموزشی)
           → SYS3.Lookup EmployeeTrainingExtra2
"""
import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_lookup_codes, sync_lookup
from utils.hcm_extra_settings import ensure_hcm_extra_fields

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_COURSE_TITLE = '-'
DEFAULT_COURSE_SUBJECT_CODE = 1  # عمومی
DEFAULT_COURSE_LOCATION_CODE = 1

# Extra1: تعلق امتیاز ← LMS_EhTakeScore
EXTRA1_NO_TAKE_SCORE = 0
EXTRA1_TAKE_SCORE = 1
EMPLOYEE_TRAINING_EXTRA1_LOOKUP = {
    EXTRA1_NO_TAKE_SCORE: 'بدون تعلق امتیاز',
    EXTRA1_TAKE_SCORE: 'تعلق امتیاز',
}

# EducationHistoryStatus codes used by LMS_Eh_GetData_API TypeOperation=104
# (status 4 = «همه» is a UI filter only, not a per-row category)
EXTRA2_AMALKARD = 0                 # عملکرد آموزشی (Certificate=0)
EXTRA2_GOZARANDE = 1                # دوره های گذرانده (Certificate=1)
EXTRA2_GOZARANDE_EFFECTIVE = 2      # گذرانده نیازمند اثربخشی
EXTRA2_HAS_SCORE = 3                # دارای امتیاز (TakeScore=1)
EXTRA2_GOZARANDE_NO_CERT = 5        # گذرانده بدون گواهینامه (Present=1, Certificate=0)

EMPLOYEE_TRAINING_EXTRA2_LOOKUP = {
    EXTRA2_AMALKARD: 'عملکرد آموزشی',
    EXTRA2_GOZARANDE: 'دوره های گذرانده',
    EXTRA2_GOZARANDE_EFFECTIVE: 'گذرانده نیازمند اثربخشی',
    EXTRA2_HAS_SCORE: 'دارای امتیاز',
    EXTRA2_GOZARANDE_NO_CERT: 'گذرانده بدون گواهینامه',
}


def _parse_shamsi_date(raw):
    """Parse Shamsi date strings; reject common junk placeholders."""
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


def setup_training_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'TrainingMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.TrainingMigrationMapping (
                SourceEducationHistoryID BIGINT PRIMARY KEY,
                DestEmployeeTrainingID BIGINT NOT NULL,
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


def _positive_number(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num > 0:
        return num
    return None


def _as_int_or_none(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _flag01(val):
    try:
        return 1 if int(float(val)) == 1 else 0
    except (TypeError, ValueError):
        return 0


def _take_score_extra1(take_score):
    return EXTRA1_TAKE_SCORE if _flag01(take_score) == 1 else EXTRA1_NO_TAKE_SCORE


def _education_history_status(certificate, present, take_score, need_effective):
    """
    Map a row to one EducationHistoryStatus code (Extra2).
    Priority: 2 → 5 → 1 → 3 → 0 (most specific first; 4=همه omitted).
    """
    cert = _flag01(certificate)
    present_f = _flag01(present)
    take = _flag01(take_score)
    need = _flag01(need_effective)

    if cert == 1 and need == 1:
        return EXTRA2_GOZARANDE_EFFECTIVE
    if cert == 0 and present_f == 1:
        return EXTRA2_GOZARANDE_NO_CERT
    if cert == 1:
        return EXTRA2_GOZARANDE
    if take == 1:
        return EXTRA2_HAS_SCORE
    return EXTRA2_AMALKARD


def run():
    print("\n--- Running Step 6: Employee Training Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_training_mapping_table(dest_cursor)

        print("Ensuring EmployeeTrainingExtra1 (تعلق امتیاز)...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'EmployeeTrainingExtra1',
            EMPLOYEE_TRAINING_EXTRA1_LOOKUP,
            overwrite_values=True,
        )

        print("Ensuring EmployeeTrainingExtra2 (API EducationHistoryStatus)...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'EmployeeTrainingExtra2',
            EMPLOYEE_TRAINING_EXTRA2_LOOKUP,
            overwrite_values=True,
        )
        ensure_hcm_extra_fields(
            dest_cursor, ('EmployeeTrainingExtra1', 'EmployeeTrainingExtra2')
        )

        print("Fetching Source Education / Training History...")
        source_df = pd.read_sql("""
            SELECT
                eh.LMS_EhID AS SourceEducationHistoryID,
                eh.Tbl_PersonnelId_fk AS SourceID,
                eh.LMS_EhStartDate AS StartDate,
                eh.LMS_EhEndDate AS EndDate,
                eh.LMS_EhExecuteDate AS ExecuteDate,
                eh.LMS_EhTime AS EhTime,
                eh.LMS_EhScore AS EhScore,
                eh.LMS_EhAverage AS EhAverage,
                eh.LMS_EhCertificate AS HasCertificate,
                eh.LMS_EhPresent AS EhPresent,
                eh.LMS_EhTakeScore AS EhTakeScore,
                eh.LMS_EhRelationStatus AS RelationStatus,
                eh.LMS_EhCertificateType_fk AS CertificateTypeID,
                b.LMS_LmsBaseName AS CertificateTypeName,
                cl.LMS_ClassNeedEffective AS ClassNeedEffective,
                c.LMS_CourseName AS CourseName,
                c.LMS_CourseDuration AS CourseDuration
            FROM dbo.LMS_EducationHistory eh
            LEFT JOIN dbo.LMS_Course c ON c.LMS_CourseID = eh.LMS_CourseID_fk
            LEFT JOIN dbo.LMS_Class cl ON cl.LMS_ClassID = eh.LMS_ClassID_fk
            LEFT JOIN dbo.LMS_LmsBase b ON b.LMS_LmsBaseID = eh.LMS_EhCertificateType_fk
            WHERE eh.Tbl_PersonnelId_fk IS NOT NULL
              AND eh.Tbl_PersonnelId_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No training history rows found.")
            dest_cnxn.commit()
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
            dest_cnxn.commit()
            return

        print("Syncing CourseSubject from certificate types (نوع گواهینامه)...")
        work_df['CertificateTypeClean'] = work_df['CertificateTypeName'].apply(clean_persian_text)
        cert_names = [
            n for n in work_df['CertificateTypeClean'].dropna().unique().tolist() if n
        ]
        catalog_df = pd.read_sql("""
            SELECT LMS_LmsBaseName
            FROM dbo.LMS_LmsBase
            WHERE LMS_LmsBaseParentID_fk = 24
              AND LMS_LmsBaseActive = 1
        """, source_cnxn)
        for raw in catalog_df['LMS_LmsBaseName'].dropna().unique():
            name = clean_persian_text(raw)
            if name and name not in cert_names:
                cert_names.append(name)

        name_to_subject = sync_lookup(
            dest_cnxn, dest_cursor, 'CourseSubject', cert_names
        )
        id_to_subject = {}
        for _, row in work_df[['CertificateTypeID', 'CertificateTypeClean']].drop_duplicates().iterrows():
            cid = _as_int_or_none(row['CertificateTypeID'])
            name = row['CertificateTypeClean']
            if cid is None or not name:
                continue
            code = name_to_subject.get(normalize_persian(name))
            if code is not None:
                id_to_subject[cid] = int(code)

        mapped_df = pd.read_sql("""
            SELECT SourceEducationHistoryID, DestEmployeeTrainingID
            FROM master.dbo.TrainingMigrationMapping
        """, dest_cnxn)
        already_mapped = {}
        if not mapped_df.empty:
            already_mapped = {
                int(r['SourceEducationHistoryID']): int(r['DestEmployeeTrainingID'])
                for _, r in mapped_df.iterrows()
            }

        print("Preparing ID generator...")
        training_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeTraining', 0)

        insert_training_sql = """
            INSERT INTO HCM3.EmployeeTraining (
                EmployeeTrainingID, EmployeeRef, CourseTitle, StartDate, EndDate,
                Duration, CourseLocationCode, CourseSubjectCode, EffectiveDate,
                TrainingRelationTypeCode, Score, HasCertification, Internal,
                Extra1Code, Extra2Code,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.TrainingMigrationMapping (
                SourceEducationHistoryID, DestEmployeeTrainingID
            ) VALUES (?, ?)
        """
        update_sql = """
            UPDATE HCM3.EmployeeTraining
            SET CourseSubjectCode = ?,
                Extra1Code = ?,
                Extra2Code = ?,
                HasCertification = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeTrainingID = ?
        """

        inserted = 0
        fields_updated = 0
        skipped_already_mapped = 0
        defaulted_duration = 0
        defaulted_effective = 0
        defaulted_subject = 0
        extra1_take = 0
        extra1_no_take = 0
        extra2_counts = {code: 0 for code in EMPLOYEE_TRAINING_EXTRA2_LOOKUP}
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting/updating EmployeeTraining records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_eh_id = int(row['SourceEducationHistoryID'])
            cert_id = _as_int_or_none(row['CertificateTypeID'])
            subject_code = id_to_subject.get(cert_id) if cert_id is not None else None
            if subject_code is None:
                subject_code = DEFAULT_COURSE_SUBJECT_CODE
                defaulted_subject += 1

            extra1_code = _take_score_extra1(row['EhTakeScore'])
            if extra1_code == EXTRA1_TAKE_SCORE:
                extra1_take += 1
            else:
                extra1_no_take += 1

            extra2_code = _education_history_status(
                row['HasCertificate'],
                row['EhPresent'],
                row['EhTakeScore'],
                row['ClassNeedEffective'],
            )
            extra2_counts[extra2_code] = extra2_counts.get(extra2_code, 0) + 1

            try:
                has_cert = int(row['HasCertificate']) == 1
            except (TypeError, ValueError):
                has_cert = False

            if source_eh_id in already_mapped:
                dest_cursor.execute(
                    update_sql,
                    (
                        subject_code,
                        extra1_code,
                        extra2_code,
                        has_cert,
                        already_mapped[source_eh_id],
                    ),
                )
                fields_updated += 1
                skipped_already_mapped += 1
                continue

            employee_id = int(row['EmployeeID'])

            course_title = clean_persian_text(row['CourseName']) or DEFAULT_COURSE_TITLE
            course_title = course_title[:400]

            start_date = _parse_shamsi_date(row['StartDate'])
            end_date = _parse_shamsi_date(row['EndDate'])
            execute_date = _parse_shamsi_date(row['ExecuteDate'])

            effective_date = execute_date or end_date or start_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            eh_time = _positive_number(row['EhTime'])
            course_duration = _positive_number(row['CourseDuration'])
            if eh_time is not None:
                duration = int(eh_time)
            elif course_duration is not None:
                duration = int(course_duration)
                defaulted_duration += 1
            else:
                duration = 0
                defaulted_duration += 1

            avg = _positive_number(row['EhAverage'])
            score_val = _positive_number(row['EhScore'])
            score = avg if avg is not None else score_val

            try:
                relation_status = int(row['RelationStatus'])
            except (TypeError, ValueError):
                relation_status = 0
            relation_code = 1 if relation_status == 1 else 2

            training_last_id += 1
            dest_cursor.execute(insert_training_sql, (
                training_last_id,
                employee_id,
                course_title,
                start_date,
                end_date,
                duration,
                DEFAULT_COURSE_LOCATION_CODE,
                subject_code,
                effective_date,
                relation_code,
                score,
                has_cert,
                extra1_code,
                extra2_code,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_eh_id, training_last_id))
            already_mapped[source_eh_id] = training_last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeTraining'",
            (training_last_id,),
        )

        dest_cnxn.commit()
        extra2_summary = ', '.join(
            f"{EMPLOYEE_TRAINING_EXTRA2_LOOKUP[c]}={extra2_counts.get(c, 0)}"
            for c in sorted(EMPLOYEE_TRAINING_EXTRA2_LOOKUP)
        )
        print(
            f"Success! Training records inserted: {inserted}. "
            f"Fields updated: {fields_updated}. "
            f"Extra1 تعلق امتیاز: {extra1_take}, بدون: {extra1_no_take}. "
            f"Defaulted CourseSubject: {defaulted_subject}. "
            f"Extra2: {extra2_summary}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Defaulted duration: {defaulted_duration}. "
            f"Defaulted effective date: {defaulted_effective}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Training step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
