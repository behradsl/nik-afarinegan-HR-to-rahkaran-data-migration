"""
Step 12: Propagate EmployeeStatute snapshot fields onto existing internal work records.

UPDATE only — never INSERT. Match: internal WorkType + PostRef + DepartmentRef
+ ApplyDate inside WR [StartDate, EndDate]. Latest statute wins when multiple match.
"""
import pandas as pd
import warnings
from db_core import get_connections

warnings.filterwarnings('ignore', category=UserWarning)

# Must match step_07 WORK_TYPE_INTERNAL (دولتی داخل شرکت)
WORK_TYPE_INTERNAL = 1


def run():
    print("\n--- Running Step 12: Work Record Propagation from Statutes ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()
    # Source unused for this step
    source_cnxn.close()

    try:
        print("Loading migrated internal work records...")
        wr_df = pd.read_sql("""
            SELECT
                wr.EmployeeWorkRecordID,
                wr.EmployeeRef,
                wr.PostRef,
                wr.DepartmentRef,
                wr.StartDate,
                wr.EndDate,
                wr.WorkTypeCode
            FROM HCM3.EmployeeWorkRecord wr
            INNER JOIN master.dbo.WorkRecordMigrationMapping m
                ON m.DestEmployeeWorkRecordID = wr.EmployeeWorkRecordID
            WHERE wr.WorkTypeCode = ?
              AND wr.PostRef IS NOT NULL
              AND wr.DepartmentRef IS NOT NULL
              AND wr.StartDate IS NOT NULL
        """, dest_cnxn, params=[WORK_TYPE_INTERNAL])

        if wr_df.empty:
            print("No internal migrated work records with Post/Dept/Start to update.")
            dest_cnxn.commit()
            return

        print("Loading migrated statutes...")
        st_df = pd.read_sql("""
            SELECT
                s.EmployeeStatuteID,
                s.EmployeeRef,
                s.PostRef,
                s.DepartmentRef,
                s.ApplyDate,
                s.IssueDate,
                s.Number,
                s.EmploymentTypeRef,
                s.WorkLocationCode,
                s.RankCode,
                s.BaseCode
            FROM HCM3.EmployeeStatute s
            INNER JOIN master.dbo.StatuteMigrationMapping m
                ON m.DestEmployeeStatuteID = s.EmployeeStatuteID
            WHERE s.ApplyDate IS NOT NULL
              AND s.PostRef IS NOT NULL
              AND s.DepartmentRef IS NOT NULL
        """, dest_cnxn)

        if st_df.empty:
            print("No migrated statutes with ApplyDate/Post/Dept found.")
            dest_cnxn.commit()
            return

        # Normalize dates to comparable strings YYYY-MM-DD
        for col in ('StartDate', 'EndDate'):
            wr_df[col] = wr_df[col].apply(
                lambda x: str(x)[:10] if x is not None and not (isinstance(x, float) and pd.isna(x)) else None
            )
        for col in ('ApplyDate', 'IssueDate'):
            st_df[col] = st_df[col].apply(
                lambda x: str(x)[:10] if x is not None and not (isinstance(x, float) and pd.isna(x)) else None
            )

        update_sql = """
            UPDATE HCM3.EmployeeWorkRecord
            SET StatuteNumber = ?,
                StatuteApplyDate = ?,
                EmploymentTypeRef = ?,
                WorkLocationCode = ?,
                RankCode = ?,
                BaseCode = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeWorkRecordID = ?
        """

        updated = 0
        skipped_no_match = 0

        print(f"Matching statutes to {len(wr_df)} internal work records...")
        for _, wr in wr_df.iterrows():
            wr_id = int(wr['EmployeeWorkRecordID'])
            emp = int(wr['EmployeeRef'])
            post = int(wr['PostRef'])
            dept = int(wr['DepartmentRef'])
            start = wr['StartDate']
            end = wr['EndDate']

            mask = (
                (st_df['EmployeeRef'] == emp)
                & (st_df['PostRef'] == post)
                & (st_df['DepartmentRef'] == dept)
                & (st_df['ApplyDate'] >= start)
            )
            if end:
                mask = mask & (st_df['ApplyDate'] <= end)
            candidates = st_df[mask].copy()

            if candidates.empty:
                skipped_no_match += 1
                continue

            # Latest ApplyDate; tie-break IssueDate then EmployeeStatuteID
            candidates = candidates.sort_values(
                by=['ApplyDate', 'IssueDate', 'EmployeeStatuteID'],
                ascending=[False, False, False],
                na_position='last',
            )
            best = candidates.iloc[0]

            number = best['Number']
            if number is not None and isinstance(number, float) and pd.isna(number):
                number = None
            elif number is not None:
                number = str(number)[:200]

            apply_date = best['ApplyDate']
            et_ref = int(best['EmploymentTypeRef']) if pd.notna(best['EmploymentTypeRef']) else None
            loc = int(best['WorkLocationCode']) if pd.notna(best['WorkLocationCode']) else None
            rank = int(best['RankCode']) if pd.notna(best['RankCode']) else None
            base = int(best['BaseCode']) if pd.notna(best['BaseCode']) else None

            dest_cursor.execute(
                update_sql,
                (number, apply_date, et_ref, loc, rank, base, wr_id),
            )
            updated += 1

        dest_cnxn.commit()
        print(
            f"Success! Work records updated from statutes: {updated}. "
            f"No matching statute: {skipped_no_match}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Work Record from Statute step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        dest_cnxn.close()
