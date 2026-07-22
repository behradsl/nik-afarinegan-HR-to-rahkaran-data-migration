"""
Rollback step: delete destination rows that were inserted by this migration,
using master.dbo.*MigrationMapping tables as the source of truth.

Order is reverse dependency (children before parents).
Linked (pre-existing) parties are kept; only parties created during migration
are deleted (CreationDate near Mapping.MigrationDate).
"""
import warnings
from db_core import get_connections

warnings.filterwarnings('ignore', category=UserWarning)

# Mapping table name -> (dest schema.table, dest PK, mapping dest-ID column)
DELETE_BY_MAPPING = (
    ('WarriorMigrationMapping', 'HCM3.EmployeeWarriorRecord', 'EmployeeWarriorRecordID', 'DestEmployeeWarriorRecordID'),
    ('WorkRecordMigrationMapping', 'HCM3.EmployeeWorkRecord', 'EmployeeWorkRecordID', 'DestEmployeeWorkRecordID'),
    ('TrainingMigrationMapping', 'HCM3.EmployeeTraining', 'EmployeeTrainingID', 'DestEmployeeTrainingID'),
    ('RelativeMigrationMapping', 'HCM3.EmployeeRelative', 'EmployeeRelativeID', 'DestEmployeeRelativeID'),
    ('EducationMigrationMapping', 'HCM3.EmployeeEducation', 'EmployeeEducationID', 'DestEmployeeEducationID'),
    ('PostMigrationMapping', 'HCM3.Post', 'PostID', 'DestPostID'),
    ('DepartmentMigrationMapping', 'HCM3.Department', 'DepartmentID', 'DestDepartmentID'),
)

MAPPING_TABLES_TO_CLEAR = (
    'WarriorMigrationMapping',
    'WorkRecordMigrationMapping',
    'TrainingMigrationMapping',
    'RelativeMigrationMapping',
    'EducationMigrationMapping',
    'MilitaryMigrationMapping',
    'PostMigrationMapping',
    'DepartmentMigrationMapping',
    'PartyMigrationMapping',
    'DegreeMigrationMapping',
)


def _mapping_exists(cursor, table_name):
    cursor.execute(
        "SELECT 1 FROM master.sys.tables WHERE name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _delete_joined(cursor, label, sql):
    cursor.execute(sql)
    count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    print(f"  -> {label}: deleted {count}")
    return count


def _clear_military_fields(cursor):
    if not _mapping_exists(cursor, 'MilitaryMigrationMapping'):
        print("  -> MilitaryMigrationMapping not found, skip military field clear.")
        return 0
    cursor.execute("""
        UPDATE e
        SET e.MilitaryStartDate = NULL,
            e.MilitaryEndDate = NULL,
            e.MilitaryDuration = NULL,
            e.MilitaryEducationDegreeCode = NULL,
            e.MilitaryServiceStatusCode = NULL,
            e.LastModificationDate = GETDATE(),
            e.LastModifier = 1
        FROM HCM3.Employee e
        INNER JOIN master.dbo.MilitaryMigrationMapping m
            ON e.EmployeeID = m.DestEmployeeID
    """)
    count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    print(f"  -> Military fields cleared on Employee: {count}")
    return count


def _delete_migrated_marriages(cursor):
    """Remove marriage rows created alongside migrated spouse relatives."""
    if not _mapping_exists(cursor, 'RelativeMigrationMapping'):
        print("  -> RelativeMigrationMapping not found, skip marriage cleanup.")
        return 0
    return _delete_joined(cursor, 'EmployeeMarriage (migrated)', """
        DELETE mar
        FROM HCM3.EmployeeMarriage mar
        WHERE mar.EmployeeRef IN (
            SELECT DISTINCT r.EmployeeRef
            FROM HCM3.EmployeeRelative r
            INNER JOIN master.dbo.RelativeMigrationMapping m
                ON m.DestEmployeeRelativeID = r.EmployeeRelativeID
        )
        AND mar.CreationDate >= (
            SELECT MIN(MigrationDate)
            FROM master.dbo.RelativeMigrationMapping
        )
    """)


def _delete_by_mapping(cursor, mapping_table, dest_table, dest_pk, mapping_col):
    if not _mapping_exists(cursor, mapping_table):
        print(f"  -> {mapping_table} not found, skip {dest_table}.")
        return 0
    return _delete_joined(cursor, dest_table, f"""
        DELETE d
        FROM {dest_table} d
        INNER JOIN master.dbo.{mapping_table} m
            ON d.{dest_pk} = m.{mapping_col}
    """)


def _delete_migrated_employees(cursor):
    """
    Delete employees created for mapped parties:
    - party inserted by migration, or
    - employee created at/after the party mapping time (linked party, new employee).
    Pre-existing employees on linked parties are kept.
    """
    if not _mapping_exists(cursor, 'PartyMigrationMapping'):
        print("  -> PartyMigrationMapping not found, skip Employee cleanup.")
        return 0
    return _delete_joined(cursor, 'HCM3.Employee (migrated)', """
        DELETE e
        FROM HCM3.Employee e
        INNER JOIN master.dbo.PartyMigrationMapping m
            ON e.PartyRef = m.DestPartyID
        INNER JOIN GNR3.Party p
            ON p.PartyID = m.DestPartyID
        WHERE p.CreationDate >= DATEADD(MINUTE, -10, m.MigrationDate)
           OR e.CreationDate >= DATEADD(MINUTE, -10, m.MigrationDate)
    """)


def _delete_migrated_parties(cursor):
    """Delete only parties inserted by migration (CreationDate near mapping time)."""
    if not _mapping_exists(cursor, 'PartyMigrationMapping'):
        print("  -> PartyMigrationMapping not found, skip Party cleanup.")
        return 0
    return _delete_joined(cursor, 'GNR3.Party (inserted)', """
        DELETE p
        FROM GNR3.Party p
        INNER JOIN master.dbo.PartyMigrationMapping m
            ON p.PartyID = m.DestPartyID
        WHERE p.CreationDate >= DATEADD(MINUTE, -10, m.MigrationDate)
          AND p.CreationDate <= DATEADD(MINUTE, 10, m.MigrationDate)
    """)


def _clear_mapping_table(cursor, table_name):
    if not _mapping_exists(cursor, table_name):
        print(f"  -> {table_name}: not found, skip.")
        return 0
    cursor.execute(f"DELETE FROM master.dbo.{table_name}")
    count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    print(f"  -> Cleared {table_name}: {count}")
    return count


def run():
    print("\n--- Running Step 0: Cleanup Migrated Destination Data ---")
    print("This deletes destination rows tracked by migration mapping tables.")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()
    # source is unused; close early
    source_cnxn.close()

    try:
        print("Clearing military fields updated by migration...")
        _clear_military_fields(dest_cursor)

        print("Deleting marriage history created with relatives...")
        _delete_migrated_marriages(dest_cursor)

        print("Deleting mapped child / org records...")
        for mapping_table, dest_table, dest_pk, mapping_col in DELETE_BY_MAPPING:
            _delete_by_mapping(dest_cursor, mapping_table, dest_table, dest_pk, mapping_col)

        print("Deleting migrated employees...")
        _delete_migrated_employees(dest_cursor)

        print("Deleting parties inserted by migration (linked parties kept)...")
        _delete_migrated_parties(dest_cursor)

        print("Clearing mapping tables...")
        for table_name in MAPPING_TABLES_TO_CLEAR:
            _clear_mapping_table(dest_cursor, table_name)

        dest_cnxn.commit()
        print("Success! Migrated destination data and mapping tables cleaned up.")
        print("Note: SYS3.Lookup values created during sync are left in place.")

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Cleanup failed. Transaction rolled back. Error: {e}")
        raise e
    finally:
        dest_cnxn.close()
