"""
Step 13: Migrate HRS_OtherExtraHistory (سایر فوق العاده ها)
into HCM3.EmployeeResearch, syncing PAY_PayrollFactor names into ResearchType.
"""
import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import sync_lookup

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


def setup_research_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'ResearchMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.ResearchMigrationMapping (
                SourceOtherExtraHistoryID BIGINT PRIMARY KEY,
                DestEmployeeResearchID BIGINT NOT NULL,
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
    """Use source score as-is when numeric (including 0)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def run():
    print("\n--- Running Step 13: Other Extras → EmployeeResearch Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_research_mapping_table(dest_cursor)

        print("Fetching Source Other Extra History...")
        source_df = pd.read_sql("""
            SELECT
                o.HRS_OehID AS SourceOtherExtraHistoryID,
                o.TBL_PersonnelID_fk AS SourceID,
                o.PAY_PfID_fk AS SourceFactorID,
                o.HRS_OehStartDate AS StartDate,
                o.HRS_OehEndDate AS EndDate,
                o.HRS_OehScore AS OehScore,
                o.HRS_OehNote AS OehNote,
                pf.PAY_PfName AS FactorName
            FROM dbo.HRS_OtherExtraHistory o
            LEFT JOIN dbo.PAY_PayrollFactor pf ON pf.PAY_PfID = o.PAY_PfID_fk
            WHERE o.HRS_OehID > 0
              AND o.TBL_PersonnelID_fk IS NOT NULL
              AND o.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No other-extra history rows found.")
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

        work_df['FactorNameClean'] = work_df['FactorName'].apply(clean_persian_text)
        skipped_no_factor = int(work_df['FactorNameClean'].isna().sum())
        work_df = work_df[work_df['FactorNameClean'].notna()].copy()

        if work_df.empty:
            print(
                f"No usable payroll factors. "
                f"Skipped (no employee): {skipped_no_employee}. "
                f"Skipped (no factor): {skipped_no_factor}."
            )
            return

        print("Syncing ResearchType lookups from payroll factors...")
        name_to_code = sync_lookup(
            dest_cnxn,
            dest_cursor,
            'ResearchType',
            work_df['FactorNameClean'].unique(),
        )

        mapped_df = pd.read_sql(
            "SELECT SourceOtherExtraHistoryID FROM master.dbo.ResearchMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourceOtherExtraHistoryID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Preparing ID generator...")
        research_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeResearch', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeResearch (
                EmployeeResearchID, EmployeeRef, Title, TypeCode,
                EffectiveDate, ExpiredDate, Score,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.ResearchMigrationMapping (
                SourceOtherExtraHistoryID, DestEmployeeResearchID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        skipped_no_type = 0
        defaulted_effective = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeResearch records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceOtherExtraHistoryID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            type_code = name_to_code.get(row['FactorNameClean'])
            if type_code is None:
                skipped_no_type += 1
                continue
            type_code = int(type_code)

            title = row['FactorNameClean']
            note = clean_persian_text(row['OehNote'])
            if note:
                title = f"{title} - {note}"
            title = (title or DEFAULT_TITLE)[:400]

            start_date = _parse_shamsi_date(row['StartDate'])
            end_date = _parse_shamsi_date(row['EndDate'])
            effective_date = start_date or end_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            score = _score_value(row['OehScore'])

            research_last_id += 1
            dest_cursor.execute(insert_sql, (
                research_last_id,
                int(row['EmployeeID']),
                title,
                type_code,
                effective_date,
                end_date,
                score,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_id, research_last_id))
            already_mapped.add(source_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeResearch'",
            (research_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! EmployeeResearch inserted: {inserted}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (no factor): {skipped_no_factor}. "
            f"Skipped (no type): {skipped_no_type}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Defaulted effective date: {defaulted_effective}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during EmployeeResearch step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
