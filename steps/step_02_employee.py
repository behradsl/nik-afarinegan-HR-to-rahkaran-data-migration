import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, clean_persian_text
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)


def run():
    print("\n--- Running Step 2: Employee Role Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        print("Fetching Source IDs, Codes, and Death Dates...")
        source_df = pd.read_sql("""
            SELECT
                TBL_PersonnelID AS SourceID,
                TBL_PersonnelID AS Code,
                TBL_PersonnelDeathDate AS DeathDate
            FROM dbo.TBL_Personnel
        """, source_cnxn)

        mapping_df = pd.read_sql(
            "SELECT SourceID, DestPartyID FROM master.dbo.PartyMigrationMapping",
            dest_cnxn,
        )
        party_df = pd.read_sql(
            "SELECT PartyID, FirstName, LastName, FullName FROM GNR3.Party",
            dest_cnxn,
        )
        existing_emp_df = pd.read_sql(
            "SELECT EmployeeID, PartyRef, Code FROM HCM3.Employee",
            dest_cnxn,
        )

        print("Transforming and Joining Data in Memory...")
        merged_df = pd.merge(source_df, mapping_df, on='SourceID', how='inner')
        final_df = pd.merge(
            merged_df, party_df, left_on='DestPartyID', right_on='PartyID', how='inner'
        )

        party_to_emp = {}
        code_to_emp = {}
        for _, row in existing_emp_df.iterrows():
            emp_id = int(row['EmployeeID'])
            if pd.notna(row['PartyRef']):
                party_to_emp.setdefault(int(row['PartyRef']), emp_id)
            if pd.notna(row['Code']) and str(row['Code']).strip():
                code_to_emp.setdefault(str(row['Code']).strip(), emp_id)

        update_sql = """
            UPDATE HCM3.Employee
            SET FirstName = ?, LastName = ?, DeathDate = ?,
                LastModificationDate = GETDATE(), LastModifier = 1
            WHERE EmployeeID = ?
              AND (
                ISNULL(FirstName, N'') <> ISNULL(?, N'')
                OR ISNULL(LastName, N'') <> ISNULL(?, N'')
                OR ISNULL(DeathDate, '19000101') <> ISNULL(?, '19000101')
              )
        """
        insert_employee_sql = """
            INSERT INTO HCM3.Employee (
                EmployeeID, PartyRef, Code, Status,
                FirstName, LastName, DeathDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """

        to_insert = []
        linked = 0
        updated = 0

        for _, row in final_df.iterrows():
            party_ref = int(row['DestPartyID'])
            code_val = str(clean_value(row['Code']))
            fname = clean_persian_text(row['FirstName']) or '-'
            lname = clean_persian_text(row['LastName']) or '-'
            raw_death_date = clean_value(row['DeathDate'])
            death_date = shamsi_to_gregorian(raw_death_date) if raw_death_date else None

            existing_emp_id = party_to_emp.get(party_ref)
            if existing_emp_id is None:
                existing_emp_id = code_to_emp.get(code_val)

            if existing_emp_id is not None:
                dest_cursor.execute(
                    update_sql,
                    (fname, lname, death_date, existing_emp_id, fname, lname, death_date),
                )
                if dest_cursor.rowcount:
                    updated += 1
                linked += 1
                party_to_emp[party_ref] = existing_emp_id
                code_to_emp[code_val] = existing_emp_id
                continue

            to_insert.append(
                {
                    'PartyRef': party_ref,
                    'Code': code_val,
                    'FirstName': fname,
                    'LastName': lname,
                    'DeathDate': death_date,
                }
            )

        if not to_insert and linked == 0:
            print("No new Employee records to migrate.")
            return

        inserted = 0
        if to_insert:
            print(f"Preparing to insert {len(to_insert)} Employee records...")
            dest_cursor.execute("""
                SELECT LastId
                FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
                WHERE TableName = 'hcm3.employee'
            """)
            id_row = dest_cursor.fetchone()

            if not id_row:
                dest_cursor.execute(
                    "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('hcm3.employee', 1000)"
                )
                current_last_id = 1000
            else:
                current_last_id = id_row[0]

            for record in to_insert:
                current_last_id += 1
                dest_cursor.execute(
                    insert_employee_sql,
                    (
                        current_last_id,
                        record['PartyRef'],
                        record['Code'],
                        1,
                        record['FirstName'],
                        record['LastName'],
                        record['DeathDate'],
                    ),
                )
                party_to_emp[record['PartyRef']] = current_last_id
                code_to_emp[record['Code']] = current_last_id
                inserted += 1

            dest_cursor.execute("""
                UPDATE SYS3.tableIdGen
                SET LastId = ?
                WHERE TableName = 'HCM3.Employee'
            """, (current_last_id,))

        dest_cnxn.commit()
        print(
            f"Success! Employees inserted: {inserted}, "
            f"linked existing: {linked}, updated: {updated}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Employee insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
