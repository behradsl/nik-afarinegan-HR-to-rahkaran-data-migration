import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.org_migration import ensure_table_id

EMPLOYEE_ENTITY_NAME = 'SystemGroup.HCM.Staff,Employee'
EMPLOYEE_ENTRY_FIELD_NAME = 'تاریخ ورود به شرکت'

warnings.filterwarnings('ignore', category=UserWarning)


def _archive_code(raw):
    """کد پرونده بایگانی. Blank and 0 stay empty. WorkIDNumber is nvarchar(100)."""
    text = clean_persian_text(raw)
    if not text or text in ('0', '0.0'):
        return None
    return text[:100]


def _ensure_employee_entry_field(dest_cursor):
    """
    The dynamic employee field تاریخ ورود به شرکت (DATE, index 0).
    Returns (entity_code, field_index).
    """
    dest_cursor.execute(
        "SELECT Code FROM SYS3.EntityLookup WHERE EntityName = ?",
        (EMPLOYEE_ENTITY_NAME,),
    )
    row = dest_cursor.fetchone()
    if not row:
        raise RuntimeError(f"EntityLookup missing for {EMPLOYEE_ENTITY_NAME}")
    entity_code = int(row[0])
    dest_cursor.execute(
        """
        SELECT [Index]
        FROM SYS3.Field
        WHERE EntityCode = ? AND Name = ?
        """,
        (entity_code, EMPLOYEE_ENTRY_FIELD_NAME),
    )
    existing = dest_cursor.fetchone()
    if existing:
        return entity_code, int(existing[0])

    last_id = ensure_table_id(dest_cursor, 'SYS3.Field', 0) + 1
    dest_cursor.execute(
        """
        SELECT ISNULL(MAX([Index]), -1) + 1
        FROM SYS3.Field
        WHERE EntityCode = ?
        """,
        (entity_code,),
    )
    field_index = int(dest_cursor.fetchone()[0])
    dest_cursor.execute(
        """
        INSERT INTO SYS3.Field (
            FieldID, Name, Type, TypeMetadata, EntityCode, [Index], IsRequired
        ) VALUES (?, ?, N'DATE', N'0', ?, ?, 0)
        """,
        (last_id, EMPLOYEE_ENTRY_FIELD_NAME, entity_code, field_index),
    )
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'SYS3.Field'",
        (last_id,),
    )
    print(f"  -> Employee field created: {EMPLOYEE_ENTRY_FIELD_NAME}.")
    return entity_code, field_index


def _migrate_employee_entry_dates(source_cnxn, dest_cnxn, dest_cursor):
    """TBL_PersonnelCreateDate -> the employee DATE extra (FieldValueContainer.Date1)."""
    entity_code, field_index = _ensure_employee_entry_field(dest_cursor)
    source_df = pd.read_sql(
        """
        SELECT TBL_PersonnelID AS SourceID, TBL_PersonnelCreateDate AS CreateDate
        FROM dbo.TBL_Personnel
        """,
        source_cnxn,
    )
    dates = {}
    for row in source_df.itertuples(index=False):
        parsed = shamsi_to_gregorian(row.CreateDate)
        if parsed:
            dates[int(row.SourceID)] = parsed
    emp_df = pd.read_sql(
        "SELECT EmployeeID, Code FROM HCM3.Employee",
        dest_cnxn,
    )
    rows = []
    for row in emp_df.itertuples(index=False):
        code = clean_value(row.Code)
        if code is None:
            continue
        try:
            source_id = int(float(str(code).strip()))
        except (TypeError, ValueError):
            continue
        parsed = dates.get(source_id)
        if parsed:
            rows.append((int(row.EmployeeID), parsed))
    if not rows:
        print("  -> No employee entry dates to store.")
        return

    dest_cursor.execute(
        """
        IF OBJECT_ID('tempdb..#EmployeeEntryDate') IS NOT NULL
            DROP TABLE #EmployeeEntryDate
        CREATE TABLE #EmployeeEntryDate (
            EmployeeID BIGINT NOT NULL PRIMARY KEY,
            EntryDate DATETIME NOT NULL
        )
        """
    )
    dest_cursor.fast_executemany = True
    dest_cursor.executemany(
        "INSERT INTO #EmployeeEntryDate (EmployeeID, EntryDate) VALUES (?, ?)",
        rows,
    )
    dest_cursor.execute(
        """
        UPDATE c
        SET c.Date1 = x.EntryDate
        FROM SYS3.FieldValueContainer c
        INNER JOIN #EmployeeEntryDate x ON x.EmployeeID = c.RecordID
        WHERE c.EntityCode = ?
          AND c.[Index] = ?
          AND ISNULL(c.Date1, '19000101') <> x.EntryDate
        """,
        (entity_code, field_index),
    )
    updated = dest_cursor.rowcount
    last_id = ensure_table_id(dest_cursor, 'SYS3.FieldValueContainer', 0)
    dest_cursor.execute("SELECT ISNULL(MAX(FieldValueContainerID), 0) FROM SYS3.FieldValueContainer")
    last_id = max(last_id, int(dest_cursor.fetchone()[0]))
    dest_cursor.execute(
        """
        SET NOCOUNT ON;
        DECLARE @base BIGINT = ?;
        INSERT INTO SYS3.FieldValueContainer (
            FieldValueContainerID, EntityCode, RecordID, [Index], Date1
        )
        SELECT
            @base + ROW_NUMBER() OVER (ORDER BY x.EmployeeID),
            ?,
            x.EmployeeID,
            ?,
            x.EntryDate
        FROM #EmployeeEntryDate x
        WHERE NOT EXISTS (
            SELECT 1
            FROM SYS3.FieldValueContainer c
            WHERE c.EntityCode = ?
              AND c.RecordID = x.EmployeeID
              AND c.[Index] = ?
        );
        DECLARE @inserted INT = @@ROWCOUNT;
        UPDATE SYS3.tableIdGen
        SET LastId = @base + @inserted
        WHERE TableName = 'SYS3.FieldValueContainer';
        SELECT @inserted;
        """,
        (last_id, entity_code, field_index, entity_code, field_index),
    )
    inserted = int(dest_cursor.fetchone()[0])
    print(
        f"  -> Employee entry dates inserted: {inserted}, updated: {updated}."
    )


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
                TBL_PersonnelDeathDate AS DeathDate,
                TBL_PersonnelFolderNo AS ArchiveCode
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
            SET FirstName = ?, LastName = ?, DeathDate = ?, WorkIDNumber = ?,
                LastModificationDate = GETDATE(), LastModifier = 1
            WHERE EmployeeID = ?
              AND (
                ISNULL(FirstName, N'') <> ISNULL(?, N'')
                OR ISNULL(LastName, N'') <> ISNULL(?, N'')
                OR ISNULL(DeathDate, '19000101') <> ISNULL(?, '19000101')
                OR ISNULL(WorkIDNumber, N'') <> ISNULL(?, N'')
              )
        """
        insert_employee_sql = """
            INSERT INTO HCM3.Employee (
                EmployeeID, PartyRef, Code, Status,
                FirstName, LastName, DeathDate, WorkIDNumber,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """

        to_insert = []
        linked = 0
        updated = 0
        skipped_duplicate = 0
        # PartyRef and Code are unique on Employee. A second mapping row that
        # still points at a party or code already queued must not be inserted.
        seen_party = set()
        seen_code = set()

        for _, row in final_df.iterrows():
            party_ref = int(row['DestPartyID'])
            code_val = str(clean_value(row['Code']))
            fname = clean_persian_text(row['FirstName']) or '-'
            lname = clean_persian_text(row['LastName']) or '-'
            raw_death_date = clean_value(row['DeathDate'])
            death_date = shamsi_to_gregorian(raw_death_date) if raw_death_date else None
            archive_code = _archive_code(row['ArchiveCode'])

            existing_emp_id = party_to_emp.get(party_ref)
            if existing_emp_id is None:
                existing_emp_id = code_to_emp.get(code_val)

            if existing_emp_id is not None:
                dest_cursor.execute(
                    update_sql,
                    (
                        fname, lname, death_date, archive_code,
                        existing_emp_id, fname, lname, death_date, archive_code,
                    ),
                )
                if dest_cursor.rowcount:
                    updated += 1
                linked += 1
                party_to_emp[party_ref] = existing_emp_id
                code_to_emp[code_val] = existing_emp_id
                seen_party.add(party_ref)
                seen_code.add(code_val)
                continue

            if party_ref in seen_party or code_val in seen_code:
                skipped_duplicate += 1
                continue

            to_insert.append(
                {
                    'PartyRef': party_ref,
                    'Code': code_val,
                    'FirstName': fname,
                    'LastName': lname,
                    'DeathDate': death_date,
                    'WorkIDNumber': archive_code,
                }
            )
            seen_party.add(party_ref)
            seen_code.add(code_val)

        if not to_insert and linked == 0:
            print(
                "No new Employee records to migrate. "
                f"Skipped duplicate party or code: {skipped_duplicate}."
            )

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
                        record['WorkIDNumber'],
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

        _migrate_employee_entry_dates(source_cnxn, dest_cnxn, dest_cursor)
        dest_cnxn.commit()
        print(
            f"Success! Employees inserted: {inserted}, "
            f"linked existing: {linked}, updated: {updated}, "
            f"skipped duplicate party or code: {skipped_duplicate}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Employee insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
