import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value
from utils.date_helpers import days_between, shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, ensure_lookup_codes

warnings.filterwarnings('ignore', category=UserWarning)

# HRS_MhStatusId_fk (PayBase parent 37 وضعیت خدمت) → MilitaryBranchCode
MILITARY_BRANCH_RELATED = 1       # مرتبط (3701)
MILITARY_BRANCH_UNRELATED = 2     # غیر مرتبط (3703)
MILITARY_BRANCH_SIMILAR = 3       # مشابه (3702)

MILITARY_BRANCH_LOOKUP = {
    MILITARY_BRANCH_RELATED: 'مرتبط',
    MILITARY_BRANCH_UNRELATED: 'غیر مرتبط',
    MILITARY_BRANCH_SIMILAR: 'مشابه',
}

# Restore default ExemptionType values after earlier mistaken overwrite
EXEMPTION_TYPE_LOOKUP = {
    1: 'دائم',
    2: 'موقت',
}

MH_STATUS_TO_BRANCH = {
    3701: MILITARY_BRANCH_RELATED,
    3703: MILITARY_BRANCH_UNRELATED,
    3702: MILITARY_BRANCH_SIMILAR,
}


def _military_branch_code(mh_status_id):
    if mh_status_id is None or (isinstance(mh_status_id, float) and pd.isna(mh_status_id)):
        return None
    try:
        return MH_STATUS_TO_BRANCH.get(int(float(mh_status_id)))
    except (TypeError, ValueError):
        return None


def setup_military_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'MilitaryMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.MilitaryMigrationMapping (
                SourcePersonnelID BIGINT PRIMARY KEY,
                DestEmployeeID BIGINT NOT NULL,
                SourceMilitaryHistoryID BIGINT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _sortable_end_date(shamsi_val):
    """Return a sort key for Shamsi end dates; invalid/empty sort last."""
    greg = shamsi_to_gregorian(clean_value(shamsi_val))
    return greg if greg else ''


def run():
    print("\n--- Running Step 4: Military History Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_military_mapping_table(dest_cursor)

        print("Ensuring MilitaryBranch lookup (مرتبط / غیر مرتبط / مشابه)...")
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'MilitaryBranch',
            MILITARY_BRANCH_LOOKUP,
            overwrite_values=True,
        )
        # Undo mistaken ExemptionType overwrite from an earlier run
        ensure_lookup_codes(
            dest_cnxn,
            dest_cursor,
            'ExemptionType',
            EXEMPTION_TYPE_LOOKUP,
            overwrite_values=True,
        )
        dest_cursor.execute(
            "DELETE FROM SYS3.Lookup WHERE Type = 'ExemptionType' AND Code = 3"
        )

        print("Fetching Source Military History...")
        source_df = pd.read_sql("""
            SELECT
                mh.HRS_MhID AS MilitaryHistoryID,
                mh.TBL_PersonnelID_fk AS SourceID,
                mh.TBL_DegreeID_fk AS SourceDegreeID,
                d.TBL_DegreeName AS DegreeName,
                mh.HRS_MhStartDate AS StartDate,
                mh.HRS_MhEndDate AS EndDate,
                mh.HRS_MhStatusId_fk AS MhStatusID
            FROM dbo.HRS_MilitaryHistory mh
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = mh.TBL_DegreeID_fk
            WHERE mh.TBL_PersonnelID_fk IS NOT NULL
              AND mh.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No military history rows found.")
            return

        total_source_people = source_df['SourceID'].nunique()
        multi_row_people = int(
            (source_df.groupby('SourceID').size() > 1).sum()
        )

        print("Selecting one row per person (latest EndDate, then highest HRS_MhID)...")
        source_df['_end_sort'] = source_df['EndDate'].apply(_sortable_end_date)
        source_df = source_df.sort_values(
            by=['SourceID', '_end_sort', 'MilitaryHistoryID'],
            ascending=[True, False, False],
        )
        selected_df = source_df.drop_duplicates(subset=['SourceID'], keep='first').copy()
        selected_df = selected_df.drop(columns=['_end_sort'])

        print("Mapping Source to Rahkaran Employee IDs...")
        emp_map_df = pd.read_sql("""
            SELECT m.SourceID, e.EmployeeID
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """, dest_cnxn)

        merged_df = pd.merge(selected_df, emp_map_df, on='SourceID', how='left')
        skipped_no_employee = int(merged_df['EmployeeID'].isna().sum())
        work_df = merged_df[merged_df['EmployeeID'].notna()].copy()

        if work_df.empty:
            print(
                f"No matching employees found. "
                f"Skipped (no employee): {skipped_no_employee}. "
                f"Multi-row people collapsed: {multi_row_people}."
            )
            return

        mapped_df = pd.read_sql(
            "SELECT SourcePersonnelID FROM master.dbo.MilitaryMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourcePersonnelID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Synchronizing degree mappings...")
        degree_id_map = ensure_degree_mappings(
            source_cnxn,
            dest_cnxn,
            dest_cursor,
            work_df[['SourceDegreeID', 'DegreeName']],
        )

        update_sql = """
            UPDATE HCM3.Employee
            SET MilitaryStartDate = ?,
                MilitaryEndDate = ?,
                MilitaryDuration = ?,
                MilitaryEducationDegreeCode = ?,
                MilitaryBranchCode = ?,
                ExemptionTypeCode = NULL,
                LastModificationDate = GETDATE(),
                MilitaryServiceStatusCode = 1,
                LastModifier = 1
            WHERE EmployeeID = ?
        """
        update_mapped_sql = """
            UPDATE HCM3.Employee
            SET MilitaryDuration = ?,
                MilitaryBranchCode = ?,
                ExemptionTypeCode = NULL,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeID = ?
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.MilitaryMigrationMapping (
                SourcePersonnelID, DestEmployeeID, SourceMilitaryHistoryID
            ) VALUES (?, ?, ?)
        """

        updated = 0
        mapped_refreshed = 0
        skipped_already_mapped = 0
        skipped_bad_dates = 0

        for _, row in work_df.iterrows():
            source_id = int(row['SourceID'])
            start_date = shamsi_to_gregorian(clean_value(row['StartDate']))
            end_date = shamsi_to_gregorian(clean_value(row['EndDate']))
            # Dest MilitaryDuration is day count between start and end
            duration = days_between(start_date, end_date)
            branch_code = _military_branch_code(row.get('MhStatusID'))

            if start_date is None and end_date is None:
                skipped_bad_dates += 1
                continue

            employee_id = int(row['EmployeeID'])

            if source_id in already_mapped:
                # Refresh duration + MilitaryBranch; clear mistaken ExemptionType
                dest_cursor.execute(
                    update_mapped_sql,
                    (duration, branch_code, employee_id),
                )
                mapped_refreshed += 1
                skipped_already_mapped += 1
                continue

            degree_code = None
            try:
                source_degree_id = int(row['SourceDegreeID'])
            except (TypeError, ValueError):
                source_degree_id = 0
            if source_degree_id > 0 and source_degree_id in degree_id_map:
                degree_code = int(degree_id_map[source_degree_id])

            dest_cursor.execute(update_sql, (
                start_date,
                end_date,
                duration,
                degree_code,
                branch_code,
                employee_id,
            ))
            dest_cursor.execute(
                insert_mapping_sql,
                (source_id, employee_id, int(row['MilitaryHistoryID'])),
            )
            already_mapped.add(source_id)
            updated += 1

        dest_cnxn.commit()
        print(
            f"Success! Updated {updated} Employee military records. "
            f"Mapped refreshed (duration/branch): {mapped_refreshed}. "
            f"Source people: {total_source_people}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (bad dates): {skipped_bad_dates}. "
            f"Multi-row people collapsed: {multi_row_people}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Military update. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
