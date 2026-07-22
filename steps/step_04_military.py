import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value
from utils.date_helpers import days_between, shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings

warnings.filterwarnings('ignore', category=UserWarning)


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

        print("Fetching Source Military History...")
        source_df = pd.read_sql("""
            SELECT
                mh.HRS_MhID AS MilitaryHistoryID,
                mh.TBL_PersonnelID_fk AS SourceID,
                mh.TBL_DegreeID_fk AS SourceDegreeID,
                d.TBL_DegreeName AS DegreeName,
                mh.HRS_MhStartDate AS StartDate,
                mh.HRS_MhEndDate AS EndDate
            FROM dbo.HRS_MilitaryHistory mh
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = mh.TBL_DegreeID_fk
            WHERE mh.HRS_MhActive = 1
              AND mh.TBL_PersonnelID_fk IS NOT NULL
              AND mh.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No active military history rows found.")
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
                LastModificationDate = GETDATE(),
                MilitaryServiceStatusCode = 1,
                LastModifier = 1
            WHERE EmployeeID = ?
        """
        update_duration_sql = """
            UPDATE HCM3.Employee
            SET MilitaryDuration = ?,
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
        duration_corrected = 0
        skipped_already_mapped = 0
        skipped_bad_dates = 0

        for _, row in work_df.iterrows():
            source_id = int(row['SourceID'])
            start_date = shamsi_to_gregorian(clean_value(row['StartDate']))
            end_date = shamsi_to_gregorian(clean_value(row['EndDate']))
            # Dest MilitaryDuration is day count between start and end
            duration = days_between(start_date, end_date)

            if start_date is None and end_date is None:
                skipped_bad_dates += 1
                continue

            employee_id = int(row['EmployeeID'])

            if source_id in already_mapped:
                # Refresh duration in days for previously migrated rows
                if duration is not None:
                    dest_cursor.execute(update_duration_sql, (duration, employee_id))
                    duration_corrected += 1
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
            f"Duration corrected (days): {duration_corrected}. "
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
