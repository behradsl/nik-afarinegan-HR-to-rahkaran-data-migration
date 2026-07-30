"""
Step 3: Migrate HRS_DegreeHistory → HCM3.EmployeeEducation.

NeedLevelCode: from the latest HRS_DegreeScore row for the same
TBL_DegreeID (by HRS_DsDate, then HRS_DsID). Fallback default = 1.
Ensures EducationNeedLevel lookup codes 1–4.
"""
import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, ensure_lookup_codes, sync_lookup

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_NEED_LEVEL = 1

# SYS3.Lookup Type = EducationNeedLevel (درجه نیاز آموزشی)
EDUCATION_NEED_LEVEL_LOOKUP = {
    1: 'یک',
    2: 'دو',
    3: 'سه',
    4: 'چهار',
}


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


def _normalize_need_level(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        code = int(float(raw))
    except (TypeError, ValueError):
        return None
    if code in EDUCATION_NEED_LEVEL_LOOKUP:
        return code
    return None


def _load_latest_need_by_degree(source_cnxn):
    """
    For each TBL_DegreeID, take NeedDegree from the latest DegreeScore row
    (max HRS_DsDate, then max HRS_DsID). Prefer active rows when present.
    Returns dict: SourceDegreeID -> NeedLevelCode (1–4).
    """
    score_df = pd.read_sql("""
        SELECT
            HRS_DsID,
            TBL_DegreeID_fk AS SourceDegreeID,
            HRS_DsNeedDegree AS NeedDegree,
            HRS_DsDate AS ScoreDate,
            HRS_DsActive AS ScoreActive
        FROM dbo.HRS_DegreeScore
        WHERE TBL_DegreeID_fk > 0
          AND HRS_DsNeedDegree IS NOT NULL
    """, source_cnxn)

    if score_df.empty:
        return {}

    score_df['NeedLevel'] = score_df['NeedDegree'].apply(_normalize_need_level)
    score_df = score_df[score_df['NeedLevel'].notna()].copy()
    if score_df.empty:
        return {}

    score_df['ScoreDate'] = score_df['ScoreDate'].apply(
        lambda x: str(x).strip().split()[0] if pd.notna(x) and str(x).strip() else ''
    )
    try:
        score_df['ScoreActive'] = pd.to_numeric(score_df['ScoreActive'], errors='coerce').fillna(0).astype(int)
    except Exception:
        score_df['ScoreActive'] = 0

    # Latest: prefer active, then latest shamsi date string, then highest ID
    score_df = score_df.sort_values(
        by=['SourceDegreeID', 'ScoreActive', 'ScoreDate', 'HRS_DsID'],
        ascending=[True, False, False, False],
    )
    latest = score_df.drop_duplicates(subset=['SourceDegreeID'], keep='first')
    return {
        int(r['SourceDegreeID']): int(r['NeedLevel'])
        for _, r in latest.iterrows()
    }


def run():
    print("\n--- Running Step 3: Education Data Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_education_mapping_table(dest_cursor)

        print("Ensuring EducationNeedLevel lookup (1–4)...")
        ensure_lookup_codes(
            dest_cnxn, dest_cursor, 'EducationNeedLevel', EDUCATION_NEED_LEVEL_LOOKUP
        )

        print("Building NeedLevel map from latest DegreeScore per degree...")
        degree_need_map = _load_latest_need_by_degree(source_cnxn)
        print(f"  -> Degrees with NeedLevel from DegreeScore: {len(degree_need_map)}.")

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
            "SELECT SourceDegreeHistoryID, DestEmployeeEducationID "
            "FROM master.dbo.EducationMigrationMapping",
            dest_cnxn,
        )
        already_map = {
            int(r['SourceDegreeHistoryID']): int(r['DestEmployeeEducationID'])
            for _, r in mapped_df.iterrows()
        }

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

        print("Preparing to insert / backfill Employee Education NeedLevelCode...")
        dest_cursor.execute(
            "SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) "
            "WHERE TableName = 'hcm3.employeeeducation'"
        )
        id_row = dest_cursor.fetchone()
        current_last_id = int(id_row[0]) if id_row else 1000

        insert_edu_sql = """
            INSERT INTO HCM3.EmployeeEducation (
                EmployeeEducationID, EmployeeRef, DegreeCode, DisciplineCode, CenterCode,
                StartDate, EndDate, GPA, NeedLevelCode, QualityCode, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ISNULL(?, GETDATE()), GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.EducationMigrationMapping (
                SourceDegreeHistoryID, DestEmployeeEducationID
            ) VALUES (?, ?)
        """
        update_need_sql = """
            UPDATE HCM3.EmployeeEducation
            SET NeedLevelCode = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeEducationID = ?
        """

        inserted = 0
        backfilled = 0
        skipped_bad_degree = 0
        from_score = 0
        from_default = 0

        for _, row in merged_df.iterrows():
            source_history_id = int(row['SourceDegreeHistoryID'])

            try:
                source_degree_id = int(row['SourceDegreeID'])
            except (TypeError, ValueError):
                skipped_bad_degree += 1
                continue
            if source_degree_id <= 0 or source_degree_id not in degree_id_map:
                skipped_bad_degree += 1
                continue

            need_level = degree_need_map.get(source_degree_id)
            if need_level is None:
                need_level = DEFAULT_NEED_LEVEL
                from_default += 1
            else:
                from_score += 1

            if source_history_id in already_map:
                dest_cursor.execute(
                    update_need_sql, (need_level, already_map[source_history_id])
                )
                backfilled += 1
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
                start_date, end_date, gpa, need_level, effective_date,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_history_id, current_last_id))
            already_map[source_history_id] = current_last_id
            inserted += 1

        if inserted:
            if id_row:
                dest_cursor.execute(
                    "UPDATE SYS3.tableIdGen SET LastId = ? "
                    "WHERE TableName = 'hcm3.employeeeducation'",
                    (current_last_id,),
                )
            else:
                dest_cursor.execute(
                    "INSERT INTO SYS3.tableIdGen (TableName, LastId) "
                    "VALUES ('hcm3.employeeeducation', ?)",
                    (current_last_id,),
                )

        dest_cnxn.commit()
        print(
            f"Success! Education inserted: {inserted}. "
            f"NeedLevel backfilled: {backfilled}. "
            f"Skipped (bad degree): {skipped_bad_degree}."
        )
        print(
            f"  -> NeedLevel from latest DegreeScore: {from_score}. "
            f"Defaulted to {DEFAULT_NEED_LEVEL}: {from_default}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Education insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
