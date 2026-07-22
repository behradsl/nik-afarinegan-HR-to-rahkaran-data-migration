import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, sync_lookup

warnings.filterwarnings('ignore', category=UserWarning)


def setup_education_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'EducationMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.EducationMigrationMapping (
                SourceDegreeHistoryID BIGINT PRIMARY KEY,
                DestEmployeeEducationID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def run():
    print("\n--- Running Step 3: Education Data Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_education_mapping_table(dest_cursor)

        print("Fetching Source Education History...")
        source_query = """
            SELECT
                dh.HRS_DhID AS SourceDegreeHistoryID,
                dh.TBL_PersonnelId_fk AS SourceID,
                dh.TBL_DegreeId_fk AS SourceDegreeID,
                d.TBL_DegreeName AS DegreeName,
                db.TBL_DbName AS DisciplineName,
                uc.HRS_UcName AS CenterName,
                dh.HRS_DhAverage AS GPA,
                dh.HRS_DhEnterDate AS StartDate,
                dh.HRS_DhRecieveDate AS EndDate,
                dh.HRS_DhExcuteDate AS EffectiveDate
            FROM dbo.HRS_DegreeHistory dh
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = dh.TBL_DegreeId_fk
            LEFT JOIN dbo.TBL_DegreeBranch db ON db.TBL_DbID = dh.TBL_DbID_fk
            LEFT JOIN dbo.HRS_UnivercityCenter uc ON uc.HRS_UCId = dh.HRS_UCId_fk
            WHERE dh.TBL_PersonnelId_fk IS NOT NULL
        """
        source_df = pd.read_sql(source_query, source_cnxn)

        print("Mapping Source to Rahkaran Employee IDs...")
        mapping_query = """
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """
        emp_map_df = pd.read_sql(mapping_query, dest_cnxn)

        merged_df = pd.merge(source_df, emp_map_df, on='SourceID', how='inner')

        if merged_df.empty:
            print("No matching employees found. Skipping Education step.")
            return

        mapped_df = pd.read_sql(
            "SELECT SourceDegreeHistoryID FROM master.dbo.EducationMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourceDegreeHistoryID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Cleaning and Normalizing Text...")
        merged_df['DegreeName'] = merged_df['DegreeName'].apply(clean_persian_text)
        merged_df = merged_df.dropna(subset=['DegreeName'])

        merged_df['DisciplineName'] = merged_df['DisciplineName'].apply(
            lambda x: clean_persian_text(x) or 'نامشخص'
        )

        merged_df['CenterName'] = merged_df['CenterName'].apply(
            lambda x: clean_persian_text(x) or 'نامشخص'
        )

        print("Synchronizing Education Lookups (Degrees, Disciplines, and Centers)...")
        degree_id_map = ensure_degree_mappings(
            source_cnxn,
            dest_cnxn,
            dest_cursor,
            merged_df[['SourceDegreeID', 'DegreeName']],
        )
        discipline_map = sync_lookup(
            dest_cnxn, dest_cursor, 'EducationDiscipline', merged_df['DisciplineName'].unique()
        )
        center_map = sync_lookup(
            dest_cnxn, dest_cursor, 'EducationCenter', merged_df['CenterName'].unique()
        )

        print("Preparing to insert Employee Education records...")
        dest_cursor.execute(
            "SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) WHERE TableName = 'hcm3.employeeeducation'"
        )
        id_row = dest_cursor.fetchone()
        current_last_id = int(id_row[0]) if id_row else 1000

        insert_edu_sql = """
            INSERT INTO HCM3.EmployeeEducation (
                EmployeeEducationID, EmployeeRef, DegreeCode, DisciplineCode, CenterCode,
                StartDate, EndDate, GPA, NeedLevelCode, QualityCode, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ISNULL(?, GETDATE()), GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.EducationMigrationMapping (
                SourceDegreeHistoryID, DestEmployeeEducationID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        skipped_bad_degree = 0

        for _, row in merged_df.iterrows():
            source_history_id = int(row['SourceDegreeHistoryID'])
            if source_history_id in already_mapped:
                skipped_already_mapped += 1
                continue

            try:
                source_degree_id = int(row['SourceDegreeID'])
            except (TypeError, ValueError):
                skipped_bad_degree += 1
                continue
            if source_degree_id <= 0 or source_degree_id not in degree_id_map:
                skipped_bad_degree += 1
                continue

            emp_id = int(row['EmployeeID'])
            deg_code = int(degree_id_map[source_degree_id])
            disc_code = int(discipline_map[row['DisciplineName']])
            center_code = int(center_map[row['CenterName']])

            start_date = shamsi_to_gregorian(clean_value(row['StartDate']))
            end_date = shamsi_to_gregorian(clean_value(row['EndDate']))
            effective_date = shamsi_to_gregorian(clean_value(row['EffectiveDate']))

            gpa = None
            raw_gpa = clean_value(row['GPA'])
            if raw_gpa is not None:
                try:
                    gpa = float(raw_gpa)
                except ValueError:
                    pass

            current_last_id += 1
            dest_cursor.execute(insert_edu_sql, (
                current_last_id, emp_id, deg_code, disc_code, center_code,
                start_date, end_date, gpa, effective_date,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_history_id, current_last_id))
            already_mapped.add(source_history_id)
            inserted += 1

        if inserted == 0:
            print(
                f"No new Employee Education records to migrate. "
                f"Skipped (already mapped): {skipped_already_mapped}. "
                f"Skipped (bad degree): {skipped_bad_degree}."
            )
            dest_cnxn.commit()
            return

        if id_row:
            dest_cursor.execute(
                "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'hcm3.employeeeducation'",
                (current_last_id,),
            )
        else:
            dest_cursor.execute(
                "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('hcm3.employeeeducation', ?)",
                (current_last_id,),
            )

        dest_cnxn.commit()
        print(
            f"Success! Migrated {inserted} Education records. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Skipped (bad degree): {skipped_bad_degree}. "
            f"New LastId is {current_last_id}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Education insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
