"""
Step 14: Migrate HRS_AbetHistory (تشویق / تقدیر)
into HCM3.EmployeeRewardPunish.
"""
import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

DEFAULT_TITLE = '-'


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


def setup_reward_punish_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'RewardPunishMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.RewardPunishMigrationMapping (
                SourceAbetHistoryID BIGINT PRIMARY KEY,
                DestEmployeeRewardPunishID BIGINT NOT NULL,
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


def _score_value(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _build_description(doc_no, description, note):
    parts = []
    doc = clean_persian_text(doc_no) if doc_no is not None else None
    if doc and doc != '0':
        parts.append(f"شماره: {doc}")
    desc = clean_persian_text(description)
    if desc:
        parts.append(desc)
    n = clean_persian_text(note)
    if n:
        parts.append(n)
    if not parts:
        return None
    return '\n'.join(parts)


def run():
    print("\n--- Running Step 14: Abet → EmployeeRewardPunish Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_reward_punish_mapping_table(dest_cursor)

        print("Fetching Source Abet History...")
        source_df = pd.read_sql("""
            SELECT
                a.HRS_AhID AS SourceAbetHistoryID,
                a.TBL_PersonnelID_fk AS SourceID,
                a.HRS_AhDocumentDate AS DocumentDate,
                a.HRS_AhDocumentNo AS DocumentNo,
                a.HRS_AhDescription AS Description,
                a.HRS_AhNote AS Note,
                a.HRS_AhScore AS AhScore,
                pb.HRS_PayBaseName AS AbetTypeName
            FROM dbo.HRS_AbetHistory a
            LEFT JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID = a.HRS_AbetTypeID_fk
            WHERE a.HRS_AhID > 0
              AND a.TBL_PersonnelID_fk IS NOT NULL
              AND a.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No abet history rows found.")
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

        mapped_df = pd.read_sql(
            "SELECT SourceAbetHistoryID FROM master.dbo.RewardPunishMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourceAbetHistoryID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Preparing ID generator...")
        last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeRewardPunish', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeRewardPunish (
                EmployeeRewardPunishID, EmployeeRef, Title,
                EffectiveDate, ExpiredDate, Score, Description,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.RewardPunishMigrationMapping (
                SourceAbetHistoryID, DestEmployeeRewardPunishID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        defaulted_effective = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeRewardPunish records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceAbetHistoryID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            title = clean_persian_text(row['AbetTypeName']) or DEFAULT_TITLE
            if title in ('تعاریف پایه',):
                title = DEFAULT_TITLE
            title = title[:400]

            effective_date = _parse_shamsi_date(row['DocumentDate'])
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            score = _score_value(row['AhScore'])
            description = _build_description(
                row['DocumentNo'], row['Description'], row['Note']
            )

            last_id += 1
            dest_cursor.execute(insert_sql, (
                last_id,
                int(row['EmployeeID']),
                title,
                effective_date,
                score,
                description,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
            already_mapped.add(source_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeRewardPunish'",
            (last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! EmployeeRewardPunish inserted: {inserted}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Defaulted effective date: {defaulted_effective}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during EmployeeRewardPunish step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
