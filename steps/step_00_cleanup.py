"""
Rollback step: delete destination rows inserted by this migration,
using master.dbo.*MigrationMapping tables as the source of truth.

Order is reverse dependency (children before parents).
Linked (pre-existing) parties are kept; only parties created during migration
are deleted (CreationDate near Mapping.MigrationDate).

SYS3.Lookup values (degrees, places, ranks, etc.) are left in place —
only PlaceMigrationMapping / DegreeMigrationMapping rows are cleared.
"""
import warnings
from db_core import get_connections

warnings.filterwarnings('ignore', category=UserWarning)

# Mapping table -> (dest table, dest PK, mapping dest-ID column)
# Children / dependents before parents / masters.
DELETE_BY_MAPPING = (
    ('PersonnelImageMigrationMapping', 'HCM3.EmployeeSupplementary', 'EmployeeSupplementaryID', 'DestEmployeeSupplementaryID'),
    ('WarriorMigrationMapping', 'HCM3.EmployeeWarriorRecord', 'EmployeeWarriorRecordID', 'DestEmployeeWarriorRecordID'),
    ('WorkRecordMigrationMapping', 'HCM3.EmployeeWorkRecord', 'EmployeeWorkRecordID', 'DestEmployeeWorkRecordID'),
    ('StatuteMigrationMapping', 'HCM3.EmployeeStatute', 'EmployeeStatuteID', 'DestEmployeeStatuteID'),
    ('OrgStructureMigrationMapping', 'HCM3.OrganizationalStructure', 'OrganizationalStructureID', 'DestOrganizationalStructureID'),
    ('ResearchMigrationMapping', 'HCM3.EmployeeResearch', 'EmployeeResearchID', 'DestEmployeeResearchID'),
    ('RewardPunishMigrationMapping', 'HCM3.EmployeeRewardPunish', 'EmployeeRewardPunishID', 'DestEmployeeRewardPunishID'),
    ('AppraisalMigrationMapping', 'HCM3.EmployeeAppraisal', 'EmployeeAppraisalID', 'DestEmployeeAppraisalID'),
    ('TrainingMigrationMapping', 'HCM3.EmployeeTraining', 'EmployeeTrainingID', 'DestEmployeeTrainingID'),
    ('RelativeInsuranceMigrationMapping', 'HCM3.EmployeeRelativeInsurance', 'EmployeeRelativeInsuranceID', 'DestEmployeeRelativeInsuranceID'),
    ('RelativeMigrationMapping', 'HCM3.EmployeeRelative', 'EmployeeRelativeID', 'DestEmployeeRelativeID'),
    ('EducationMigrationMapping', 'HCM3.EmployeeEducation', 'EmployeeEducationID', 'DestEmployeeEducationID'),
    ('StatuteTypeMigrationMapping', 'HCM3.StatuteType', 'StatuteTypeID', 'DestStatuteTypeID'),
    ('StatuteFactorMigrationMapping', 'HCM3.StatuteFactor', 'StatuteFactorID', 'DestStatuteFactorID'),
    ('JobMigrationMapping', 'HCM3.Job', 'JobID', 'DestJobID'),
    ('EmploymentTypeMigrationMapping', 'HCM3.EmploymentType', 'EmploymentTypeID', 'DestEmploymentTypeID'),
    ('PostMigrationMapping', 'HCM3.Post', 'PostID', 'DestPostID'),
    ('DepartmentMigrationMapping', 'HCM3.Department', 'DepartmentID', 'DestDepartmentID'),
)

MAPPING_TABLES_TO_CLEAR = (
    'WarriorMigrationMapping',
    'WorkRecordMigrationMapping',
    'StatuteMigrationMapping',
    'OrgStructureMigrationMapping',
    'OrgStructureDescriptionMigrationMapping',
    'StatuteTypeMigrationMapping',
    'StatuteFactorMigrationMapping',
    'ResearchMigrationMapping',
    'RewardPunishMigrationMapping',
    'AppraisalMigrationMapping',
    'AddressMigrationMapping',
    'PersonnelImageMigrationMapping',
    'TrainingMigrationMapping',
    'RelativeInsuranceMigrationMapping',
    'RelativeMigrationMapping',
    'EducationMigrationMapping',
    'MilitaryMigrationMapping',
    'JobMigrationMapping',
    'EmploymentTypeMigrationMapping',
    'PlaceMigrationMapping',
    'PlaceRegionalDivisionMapping',
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


def _exec_count(cursor, label, sql):
    cursor.execute(sql)
    count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    print(f"  -> {label}: {count}")
    return count


def _clear_military_fields(cursor):
    if not _mapping_exists(cursor, 'MilitaryMigrationMapping'):
        print("  -> MilitaryMigrationMapping not found, skip military field clear.")
        return 0
    return _exec_count(cursor, 'Military fields cleared on Employee', """
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


def _delete_migrated_addresses(cursor):
    """Delete PartyAddress then Address created by address migration."""
    if not _mapping_exists(cursor, 'AddressMigrationMapping'):
        print("  -> AddressMigrationMapping not found, skip addresses.")
        return 0
    pa = _delete_joined(cursor, 'PartyAddress (migrated)', """
        DELETE pa
        FROM GNR3.PartyAddress pa
        INNER JOIN master.dbo.AddressMigrationMapping m
            ON pa.PartyAddressID = m.DestPartyAddressID
    """)
    addr = _delete_joined(cursor, 'Address (migrated)', """
        DELETE a
        FROM GNR3.Address a
        INNER JOIN master.dbo.AddressMigrationMapping m
            ON a.AddressID = m.DestAddressID
    """)
    return pa + addr


def _delete_migrated_statute_factors(cursor):
    """
    Delete dependents of migrated StatuteFactors, then the factors themselves.
    Safe when property/value steps are added later.
    """
    if not _mapping_exists(cursor, 'StatuteFactorMigrationMapping'):
        print("  -> StatuteFactorMigrationMapping not found, skip statute factors.")
        return 0

    for label, sql in (
        ('EmployeeStatuteFactor (migrated factors)', """
            DELETE esf
            FROM HCM3.EmployeeStatuteFactor esf
            INNER JOIN master.dbo.StatuteFactorMigrationMapping m
                ON esf.StatuteFactorRef = m.DestStatuteFactorID
        """),
        ('StatuteTypeFactor (migrated factors)', """
            DELETE stf
            FROM HCM3.StatuteTypeFactor stf
            INNER JOIN master.dbo.StatuteFactorMigrationMapping m
                ON stf.StatuteFactorRef = m.DestStatuteFactorID
        """),
        ('StatuteFactorProperty (migrated factors)', """
            DELETE sfp
            FROM HCM3.StatuteFactorProperty sfp
            INNER JOIN master.dbo.StatuteFactorMigrationMapping m
                ON sfp.StatuteFactorRef = m.DestStatuteFactorID
        """),
        ('StatuteFactorDisplayOrder (migrated factors)', """
            DELETE o
            FROM HCM3.StatuteFactorDisplayOrder o
            INNER JOIN master.dbo.StatuteFactorMigrationMapping m
                ON o.StatuteFactorRef = m.DestStatuteFactorID
        """),
    ):
        _delete_joined(cursor, label, sql)

    return _delete_by_mapping(
        cursor,
        'StatuteFactorMigrationMapping',
        'HCM3.StatuteFactor',
        'StatuteFactorID',
        'DestStatuteFactorID',
    )


def _prepare_org_structure_delete(cursor):
    """Break OS self-FK, delete items, clear statute OS refs, delete descriptions."""
    if _mapping_exists(cursor, 'OrgStructureMigrationMapping'):
        _exec_count(cursor, 'OrganizationalStructure ParentRef cleared', """
            UPDATE os
            SET os.ParentRef = NULL
            FROM HCM3.OrganizationalStructure os
            INNER JOIN master.dbo.OrgStructureMigrationMapping m
                ON os.OrganizationalStructureID = m.DestOrganizationalStructureID
        """)
        _exec_count(cursor, 'OrganizationalStructureItem deleted', """
            DELETE i
            FROM HCM3.OrganizationalStructureItem i
            INNER JOIN master.dbo.OrgStructureMigrationMapping m
                ON i.OrganizationalStructureRef = m.DestOrganizationalStructureID
        """)
        _exec_count(cursor, 'EmployeeStatute.OrganizationalStructureRef cleared', """
            UPDATE s
            SET s.OrganizationalStructureRef = NULL
            FROM HCM3.EmployeeStatute s
            INNER JOIN master.dbo.OrgStructureMigrationMapping m
                ON s.OrganizationalStructureRef = m.DestOrganizationalStructureID
        """)

    if _mapping_exists(cursor, 'OrgStructureDescriptionMigrationMapping'):
        _delete_by_mapping(
            cursor,
            'OrgStructureDescriptionMigrationMapping',
            'HCM3.OrganizationalStructureDescription',
            'OrganizationalStructureDescriptionID',
            'DestDescriptionID',
        )


def _null_master_fks_before_delete(cursor):
    """
    Clear FKs from remaining rows onto masters we are about to delete.
    Main child tables are deleted earlier; this is a safety net.
    """
    if _mapping_exists(cursor, 'JobMigrationMapping'):
        _exec_count(cursor, 'WR JobRef/JobRankRef cleared', """
            UPDATE wr
            SET wr.JobRef = NULL, wr.JobRankRef = NULL,
                wr.LastModificationDate = GETDATE(), wr.LastModifier = 1
            FROM HCM3.EmployeeWorkRecord wr
            WHERE wr.JobRef IN (SELECT DestJobID FROM master.dbo.JobMigrationMapping)
               OR wr.JobRankRef IN (SELECT DestJobID FROM master.dbo.JobMigrationMapping)
        """)
        _exec_count(cursor, 'Statute JobRef cleared', """
            UPDATE s
            SET s.JobRef = NULL,
                s.LastModificationDate = GETDATE(), s.LastModifier = 1
            FROM HCM3.EmployeeStatute s
            WHERE s.JobRef IN (SELECT DestJobID FROM master.dbo.JobMigrationMapping)
        """)

    if _mapping_exists(cursor, 'EmploymentTypeMigrationMapping'):
        _exec_count(cursor, 'WR EmploymentTypeRef cleared', """
            UPDATE wr
            SET wr.EmploymentTypeRef = NULL,
                wr.LastModificationDate = GETDATE(), wr.LastModifier = 1
            FROM HCM3.EmployeeWorkRecord wr
            WHERE wr.EmploymentTypeRef IN (
                SELECT DestEmploymentTypeID FROM master.dbo.EmploymentTypeMigrationMapping
            )
        """)
        _exec_count(cursor, 'Statute EmploymentTypeRef cleared', """
            UPDATE s
            SET s.EmploymentTypeRef = NULL,
                s.LastModificationDate = GETDATE(), s.LastModifier = 1
            FROM HCM3.EmployeeStatute s
            WHERE s.EmploymentTypeRef IN (
                SELECT DestEmploymentTypeID FROM master.dbo.EmploymentTypeMigrationMapping
            )
        """)

    if _mapping_exists(cursor, 'PostMigrationMapping'):
        _exec_count(cursor, 'WR/Statute PostRef cleared', """
            UPDATE wr
            SET wr.PostRef = NULL,
                wr.LastModificationDate = GETDATE(), wr.LastModifier = 1
            FROM HCM3.EmployeeWorkRecord wr
            WHERE wr.PostRef IN (SELECT DestPostID FROM master.dbo.PostMigrationMapping)
        """)
        _exec_count(cursor, 'Statute PostRef cleared', """
            UPDATE s
            SET s.PostRef = NULL,
                s.LastModificationDate = GETDATE(), s.LastModifier = 1
            FROM HCM3.EmployeeStatute s
            WHERE s.PostRef IN (SELECT DestPostID FROM master.dbo.PostMigrationMapping)
        """)

    if _mapping_exists(cursor, 'DepartmentMigrationMapping'):
        _exec_count(cursor, 'WR DepartmentRef cleared', """
            UPDATE wr
            SET wr.DepartmentRef = NULL,
                wr.LastModificationDate = GETDATE(), wr.LastModifier = 1
            FROM HCM3.EmployeeWorkRecord wr
            WHERE wr.DepartmentRef IN (
                SELECT DestDepartmentID FROM master.dbo.DepartmentMigrationMapping
            )
        """)
        _exec_count(cursor, 'Statute DepartmentRef cleared', """
            UPDATE s
            SET s.DepartmentRef = NULL,
                s.LastModificationDate = GETDATE(), s.LastModifier = 1
            FROM HCM3.EmployeeStatute s
            WHERE s.DepartmentRef IN (
                SELECT DestDepartmentID FROM master.dbo.DepartmentMigrationMapping
            )
        """)

    if _mapping_exists(cursor, 'StatuteTypeMigrationMapping'):
        _exec_count(cursor, 'Statute StatuteTypeRef cleared', """
            UPDATE s
            SET s.StatuteTypeRef = NULL,
                s.LastModificationDate = GETDATE(), s.LastModifier = 1
            FROM HCM3.EmployeeStatute s
            WHERE s.StatuteTypeRef IN (
                SELECT DestStatuteTypeID FROM master.dbo.StatuteTypeMigrationMapping
            )
        """)


def _delete_relative_insurance_fallback(cursor):
    """
    Delete insurance rows tied to migrated relatives if insurance mapping is missing
    but relative mapping exists (older runs).
    """
    if _mapping_exists(cursor, 'RelativeInsuranceMigrationMapping'):
        return 0
    if not _mapping_exists(cursor, 'RelativeMigrationMapping'):
        return 0
    return _delete_joined(cursor, 'EmployeeRelativeInsurance (via relatives)', """
        DELETE ins
        FROM HCM3.EmployeeRelativeInsurance ins
        INNER JOIN master.dbo.RelativeMigrationMapping m
            ON ins.EmployeeRelativeRef = m.DestEmployeeRelativeID
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
    source_cnxn.close()

    try:
        print("Clearing military fields updated by migration...")
        _clear_military_fields(dest_cursor)

        print("Deleting marriage history created with relatives...")
        _delete_migrated_marriages(dest_cursor)

        print("Preparing org-structure / description cleanup...")
        _prepare_org_structure_delete(dest_cursor)

        print("Deleting mapped child records (history / statutes / structure)...")
        # Delete through OrgStructure in DELETE_BY_MAPPING; stop before masters
        child_tables = {
            'PersonnelImageMigrationMapping',
            'WarriorMigrationMapping',
            'WorkRecordMigrationMapping',
            'StatuteMigrationMapping',
            'OrgStructureMigrationMapping',
            'ResearchMigrationMapping',
            'RewardPunishMigrationMapping',
            'AppraisalMigrationMapping',
            'TrainingMigrationMapping',
            'RelativeInsuranceMigrationMapping',
            'RelativeMigrationMapping',
            'EducationMigrationMapping',
        }
        for mapping_table, dest_table, dest_pk, mapping_col in DELETE_BY_MAPPING:
            if mapping_table in child_tables:
                if mapping_table == 'RelativeInsuranceMigrationMapping':
                    _delete_relative_insurance_fallback(dest_cursor)
                _delete_by_mapping(
                    dest_cursor, mapping_table, dest_table, dest_pk, mapping_col
                )

        print("Deleting migrated party addresses...")
        _delete_migrated_addresses(dest_cursor)

        print("Clearing FKs onto masters before master delete...")
        _null_master_fks_before_delete(dest_cursor)

        print("Deleting migrated statute factors...")
        _delete_migrated_statute_factors(dest_cursor)

        print("Deleting mapped masters (statute type / job / ET / post / dept)...")
        master_tables = {
            'StatuteTypeMigrationMapping',
            'JobMigrationMapping',
            'EmploymentTypeMigrationMapping',
            'PostMigrationMapping',
            'DepartmentMigrationMapping',
        }
        for mapping_table, dest_table, dest_pk, mapping_col in DELETE_BY_MAPPING:
            if mapping_table in master_tables:
                _delete_by_mapping(
                    dest_cursor, mapping_table, dest_table, dest_pk, mapping_col
                )

        print("Deleting migrated employees...")
        _delete_migrated_employees(dest_cursor)

        print("Deleting parties inserted by migration (linked parties kept)...")
        _delete_migrated_parties(dest_cursor)

        print("Clearing mapping tables...")
        for table_name in MAPPING_TABLES_TO_CLEAR:
            _clear_mapping_table(dest_cursor, table_name)

        dest_cnxn.commit()
        print("Success! Migrated destination data and mapping tables cleaned up.")
        print(
            "Note: SYS3.Lookup values (WorkLocation, Rank, EducationDegree, etc.) "
            "created during sync are left in place."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Cleanup failed. Transaction rolled back. Error: {e}")
        raise e
    finally:
        dest_cnxn.close()
