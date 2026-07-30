"""
Step 18: Migrate TBL_PersonnelImage → HCM3.EmployeeSupplementary.Photo.

TBL_PiCode = TBL_PersonnelID. Links via PartyMigrationMapping → Employee.
Inserts or updates EmployeeSupplementary.Photo; tracks via mapping for cleanup/re-run.
"""
import warnings
from db_core import get_connections

warnings.filterwarnings('ignore', category=UserWarning)

TABLE_IDGEN = 'HCM3.EmployeeSupplementary'


def setup_personnel_image_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'PersonnelImageMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.PersonnelImageMigrationMapping (
                SourcePersonnelImageID INT PRIMARY KEY,
                SourcePersonnelID BIGINT NOT NULL,
                DestEmployeeSupplementaryID BIGINT NOT NULL,
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


def run():
    print("\n--- Running Step 18: Personnel Photo Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    source_cursor = source_cnxn.cursor()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_personnel_image_mapping_table(dest_cursor)

        print("Loading party/employee mappings...")
        dest_cursor.execute("""
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            INNER JOIN HCM3.Employee e ON e.PartyRef = m.DestPartyID
        """)
        source_to_employee = {
            int(r[0]): int(r[1]) for r in dest_cursor.fetchall()
        }
        if not source_to_employee:
            print("No mapped employees found. Skipping photo migration.")
            return

        dest_cursor.execute("""
            SELECT SourcePersonnelImageID, DestEmployeeSupplementaryID
            FROM master.dbo.PersonnelImageMigrationMapping
        """)
        already_map = {
            int(r[0]): int(r[1]) for r in dest_cursor.fetchall()
        }

        # Existing supplementary rows by employee (may pre-exist outside mapping)
        dest_cursor.execute("""
            SELECT EmployeeSupplementaryID, EmployeeRef
            FROM HCM3.EmployeeSupplementary
        """)
        emp_to_suppl = {}
        for suppl_id, emp_ref in dest_cursor.fetchall():
            emp_to_suppl[int(emp_ref)] = int(suppl_id)

        print("Fetching source personnel images...")
        source_cursor.execute("""
            SELECT
                TBL_PiID,
                TRY_CAST(TBL_PiCode AS bigint) AS SourcePersonnelID,
                TBL_PiBody
            FROM dbo.TBL_PersonnelImage
            WHERE TBL_PiActive = 1
              AND DATALENGTH(TBL_PiBody) > 0
              AND TRY_CAST(TBL_PiCode AS bigint) IS NOT NULL
              AND TRY_CAST(TBL_PiCode AS bigint) > 0
            ORDER BY TBL_PiID
        """)

        last_id = _ensure_table_id(dest_cursor, TABLE_IDGEN, 0)
        insert_sql = """
            INSERT INTO HCM3.EmployeeSupplementary (
                EmployeeSupplementaryID, EmployeeRef, Photo, Signature,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, NULL, GETDATE(), 1, GETDATE(), 1)
        """
        update_sql = """
            UPDATE HCM3.EmployeeSupplementary
            SET Photo = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeSupplementaryID = ?
        """
        map_sql = """
            INSERT INTO master.dbo.PersonnelImageMigrationMapping (
                SourcePersonnelImageID, SourcePersonnelID,
                DestEmployeeSupplementaryID, DestEmployeeID
            ) VALUES (?, ?, ?, ?)
        """

        inserted = 0
        updated = 0
        skipped_no_employee = 0
        skipped_no_body = 0

        while True:
            rows = source_cursor.fetchmany(50)
            if not rows:
                break

            for pi_id, source_personnel_id, photo_body in rows:
                source_pi_id = int(pi_id)
                source_pid = int(source_personnel_id)

                if photo_body is None:
                    skipped_no_body += 1
                    continue
                # pyodbc may return memoryview
                if isinstance(photo_body, memoryview):
                    photo_body = photo_body.tobytes()
                if not photo_body:
                    skipped_no_body += 1
                    continue

                employee_id = source_to_employee.get(source_pid)
                if employee_id is None:
                    skipped_no_employee += 1
                    continue

                if source_pi_id in already_map:
                    dest_cursor.execute(update_sql, (photo_body, already_map[source_pi_id]))
                    updated += 1
                    continue

                existing_suppl = emp_to_suppl.get(employee_id)
                if existing_suppl is not None:
                    dest_cursor.execute(update_sql, (photo_body, existing_suppl))
                    dest_cursor.execute(
                        map_sql, (source_pi_id, source_pid, existing_suppl, employee_id)
                    )
                    already_map[source_pi_id] = existing_suppl
                    updated += 1
                    continue

                last_id += 1
                dest_cursor.execute(insert_sql, (last_id, employee_id, photo_body))
                dest_cursor.execute(
                    map_sql, (source_pi_id, source_pid, last_id, employee_id)
                )
                emp_to_suppl[employee_id] = last_id
                already_map[source_pi_id] = last_id
                inserted += 1

        if inserted:
            dest_cursor.execute(
                "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
                (last_id, TABLE_IDGEN),
            )

        dest_cnxn.commit()
        print(
            f"Success! Photos inserted (new Supplementary): {inserted}. "
            f"Photos updated: {updated}. "
            f"Skipped (no mapped employee): {skipped_no_employee}. "
            f"Skipped (empty body): {skipped_no_body}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Personnel Photo step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cursor.close()
        dest_cursor.close()
        source_cnxn.close()
        dest_cnxn.close()
