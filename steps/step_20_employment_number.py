"""
Step 20: Migrate شناسه مستخدم (TBL_PersonnelPeroperty type 320)
into HCM3.EmployeeEmploymentNumber (+ Employee.EmploymentNumber current value).
"""
import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

# TBL_PersonnelPeropertyType: شناسه مستخدم
EMPLOYMENT_NUMBER_PROPERTY_TYPE = 320
DEFAULT_EFFECTIVE_DATE = '1900-01-01'


def setup_employment_number_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'EmploymentNumberMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.EmploymentNumberMigrationMapping (
                SourcePersonnelPropertyID INT PRIMARY KEY,
                DestEmployeeEmploymentNumberID BIGINT NOT NULL,
                DestEmployeeID BIGINT NOT NULL,
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


def _number_text(raw):
    text = clean_value(raw)
    if text is None:
        return None
    text = str(text).strip()
    if not text or text in ('0', 'None'):
        return None
    return text[:100]


def run():
    print("\n--- Running Step 20: Employment Number (شناسه مستخدم) Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_employment_number_mapping_table(dest_cursor)

        print("Fetching Source شناسه مستخدم (PersonnelProperty type 320)...")
        source_df = pd.read_sql(f"""
            SELECT
                pp.TBL_PpID AS SourcePersonnelPropertyID,
                pp.TBL_PersonnelID_fk AS SourceID,
                pp.TBL_PpValue AS NumberValue,
                pp.TBL_PpExecuteDate AS ExecuteDate
            FROM dbo.TBL_PersonnelPeroperty pp
            WHERE pp.TBL_PptID_fk = {EMPLOYMENT_NUMBER_PROPERTY_TYPE}
              AND ISNULL(pp.TBL_PpActive, 1) = 1
              AND pp.TBL_PersonnelID_fk IS NOT NULL
              AND pp.TBL_PersonnelID_fk > 0
              AND pp.TBL_PpValue IS NOT NULL
              AND LTRIM(RTRIM(pp.TBL_PpValue)) <> ''
              AND LTRIM(RTRIM(pp.TBL_PpValue)) <> '0'
        """, source_cnxn)

        if source_df.empty:
            print("No شناسه مستخدم property rows found.")
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

        mapped_df = pd.read_sql("""
            SELECT SourcePersonnelPropertyID, DestEmployeeEmploymentNumberID
            FROM master.dbo.EmploymentNumberMigrationMapping
        """, dest_cnxn)
        already_mapped = {}
        if not mapped_df.empty:
            already_mapped = {
                int(row['SourcePersonnelPropertyID']): int(row['DestEmployeeEmploymentNumberID'])
                for _, row in mapped_df.iterrows()
            }

        print("Preparing ID generator...")
        last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeEmploymentNumber', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeEmploymentNumber (
                EmployeeEmploymentNumberID, EmployeeRef, Number, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        update_sql = """
            UPDATE HCM3.EmployeeEmploymentNumber
            SET Number = ?,
                EffectiveDate = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeEmploymentNumberID = ?
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.EmploymentNumberMigrationMapping (
                SourcePersonnelPropertyID, DestEmployeeEmploymentNumberID, DestEmployeeID
            ) VALUES (?, ?, ?)
        """
        update_employee_sql = """
            UPDATE HCM3.Employee
            SET EmploymentNumber = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeID = ?
        """

        inserted = 0
        updated = 0
        skipped_bad_number = 0
        defaulted_dates = 0
        # employee_id -> (effective_date_str, number, sort_key)
        latest_by_employee = {}

        for _, row in work_df.iterrows():
            source_pp_id = int(row['SourcePersonnelPropertyID'])
            employee_id = int(row['EmployeeID'])
            number = _number_text(row['NumberValue'])
            if number is None:
                skipped_bad_number += 1
                continue

            effective_date = _parse_shamsi_date(row['ExecuteDate'])
            if effective_date is None:
                effective_date = DEFAULT_EFFECTIVE_DATE
                defaulted_dates += 1

            if source_pp_id in already_mapped:
                dest_id = already_mapped[source_pp_id]
                dest_cursor.execute(update_sql, (number, effective_date, dest_id))
                updated += 1
            else:
                last_id += 1
                dest_cursor.execute(
                    insert_sql,
                    (last_id, employee_id, number, effective_date),
                )
                dest_cursor.execute(
                    insert_mapping_sql,
                    (source_pp_id, last_id, employee_id),
                )
                already_mapped[source_pp_id] = last_id
                inserted += 1

            sort_key = (effective_date, source_pp_id)
            prev = latest_by_employee.get(employee_id)
            if prev is None or sort_key > prev[2]:
                latest_by_employee[employee_id] = (effective_date, number, sort_key)

        print("Updating Employee.EmploymentNumber to latest شناسه مستخدم...")
        employee_updates = 0
        for employee_id, (_eff, number, _key) in latest_by_employee.items():
            dest_cursor.execute(update_employee_sql, (number, employee_id))
            employee_updates += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeEmploymentNumber'",
            (last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Employment numbers inserted: {inserted}. "
            f"Updated: {updated}. "
            f"Employee.EmploymentNumber set: {employee_updates}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (bad number): {skipped_bad_number}. "
            f"Defaulted EffectiveDate: {defaulted_dates}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            "Migration failed during Employment Number step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
