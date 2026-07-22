import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_COURSE_TITLE = '-'


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


def run():
    print("\n--- Running Step 6: Employee Training Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_training_mapping_table(dest_cursor)

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
                eh.LMS_EhRelationStatus AS RelationStatus,
                c.LMS_CourseName AS CourseName,
                c.LMS_CourseDuration AS CourseDuration
            FROM dbo.LMS_EducationHistory eh
            LEFT JOIN dbo.LMS_Course c ON c.LMS_CourseID = eh.LMS_CourseID_fk
            WHERE eh.LMS_EhActive = 1
              AND eh.Tbl_PersonnelId_fk IS NOT NULL
              AND eh.Tbl_PersonnelId_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No active training history rows found.")
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

        mapped_df = pd.read_sql(
            "SELECT SourceEducationHistoryID FROM master.dbo.TrainingMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(mapped_df['SourceEducationHistoryID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Preparing ID generator...")
        training_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeTraining', 0)

        insert_training_sql = """
            INSERT INTO HCM3.EmployeeTraining (
                EmployeeTrainingID, EmployeeRef, CourseTitle, StartDate, EndDate,
                Duration, CourseLocationCode, CourseSubjectCode, EffectiveDate,
                TrainingRelationTypeCode, Score, HasCertification, Internal,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, 1, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.TrainingMigrationMapping (
                SourceEducationHistoryID, DestEmployeeTrainingID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        defaulted_duration = 0
        defaulted_effective = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeTraining records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_eh_id = int(row['SourceEducationHistoryID'])
            if source_eh_id in already_mapped:
                skipped_already_mapped += 1
                continue

            employee_id = int(row['EmployeeID'])

            course_title = clean_persian_text(row['CourseName']) or DEFAULT_COURSE_TITLE
            course_title = course_title[:400]

            start_date = _parse_shamsi_date(row['StartDate'])
            end_date = _parse_shamsi_date(row['EndDate'])
            execute_date = _parse_shamsi_date(row['ExecuteDate'])

            effective_date = execute_date or start_date or end_date
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
                has_cert = int(row['HasCertificate']) == 1
            except (TypeError, ValueError):
                has_cert = False

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
                effective_date,
                relation_code,
                score,
                has_cert,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_eh_id, training_last_id))
            already_mapped.add(source_eh_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeTraining'",
            (training_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Training records inserted: {inserted}. "
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
