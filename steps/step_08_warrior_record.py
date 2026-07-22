import pandas as pd
import warnings
from datetime import date
from db_core import get_connections
from utils.data_helpers import clean_value, normalize_persian
from utils.date_helpers import shamsi_to_gregorian, months_between

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'
MAX_DURATION_DAYS = 15000
DEFAULT_WARRIOR_GROUP = 3  # رزمنده

# PayBase IDs under parent 8 (نوع ایثارگری) → WarriorGroupCode
WARRIOR_GROUP_BY_PB = {
    # 1 خانواده شهید
    80003: 1, 80004: 1, 80010: 1, 80014: 1, 80015: 1, 80019: 1, 80021: 1,
    # 2 جانباز
    80002: 2, 80017: 2, 80018: 2, 80023: 2,
    # 3 رزمنده / جبهه / مناطق جنگی
    80005: 3, 80006: 3, 80009: 3, 80011: 3, 80012: 3, 80013: 3,
    80020: 3, 80022: 3,
    # 4 آزاده
    80001: 4, 80016: 4, 80024: 4,
    # 6 عضو بسیج
    80007: 6, 80008: 6, 800073: 6,
}


def _parse_shamsi_date(raw, *, treat_open_end_as_null=False):
    """Parse Shamsi date strings; reject junk; optionally treat open-ended as NULL."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    date_part = text.split()[0]
    if date_part in ('', '0', '____/__/__', '/  /', '//', '0/0/0'):
        return None
    if treat_open_end_as_null and date_part == OPEN_END_SHAMSI:
        return None
    if '_' in date_part or date_part.count('/') != 2:
        return None
    return shamsi_to_gregorian(date_part)


def setup_warrior_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'WarriorMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.WarriorMigrationMapping (
                SourceSacrificeHistoryID BIGINT PRIMARY KEY,
                DestEmployeeWarriorRecordID BIGINT NOT NULL,
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


def _as_int_or_none(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _build_paybase_parent_map(source_cnxn):
    df = pd.read_sql("""
        SELECT HRS_PayBaseID, HRS_PayBaseParentID_fk, HRS_PayBaseName
        FROM dbo.HRS_PayBase
    """, source_cnxn)
    parent_map = {}
    name_map = {}
    for _, row in df.iterrows():
        pb_id = int(row['HRS_PayBaseID'])
        parent = row['HRS_PayBaseParentID_fk']
        parent_map[pb_id] = int(parent) if parent is not None and not pd.isna(parent) else None
        name = clean_value(row['HRS_PayBaseName'])
        name_map[pb_id] = normalize_persian(str(name).strip()) if name else None
    return parent_map, name_map


def _warrior_group_code(pb_id, parent_map):
    """Walk PayBase parents; return (WarriorGroupCode, used_default)."""
    if pb_id is None or pb_id <= 0:
        return DEFAULT_WARRIOR_GROUP, True
    seen = set()
    current = int(pb_id)
    while current and current not in seen:
        seen.add(current)
        if current in WARRIOR_GROUP_BY_PB:
            return WARRIOR_GROUP_BY_PB[current], False
        current = parent_map.get(current)
        if current is None or current == 0 or current == 8:
            break
    return DEFAULT_WARRIOR_GROUP, True


def _activity_level_code(warrior_group, title):
    title_norm = title or ''
    has_active = 'فعال' in title_norm
    if warrior_group == 6:
        return 2 if has_active else 1
    return None


def _resolve_duration(start_date, end_date, duration_day_raw):
    if start_date and end_date:
        months = months_between(start_date, end_date)
        if months is not None:
            return months
    day = _as_int_or_none(duration_day_raw)
    if day is not None and 0 < day < MAX_DURATION_DAYS:
        return day
    return None


def _sacrifice_percent(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _build_description(title, note):
    parts = []
    if title:
        parts.append(title)
    note_clean = clean_value(note)
    if note_clean is not None:
        note_norm = normalize_persian(str(note_clean).strip()) or None
        if note_norm and note_norm not in parts:
            parts.append(note_norm)
    if not parts:
        return None
    return ' | '.join(parts)


def run():
    print("\n--- Running Step 8: Employee Warrior Record Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_warrior_mapping_table(dest_cursor)

        print("Loading PayBase hierarchy...")
        parent_map, name_map = _build_paybase_parent_map(source_cnxn)

        print("Fetching Source Sacrifice History...")
        source_df = pd.read_sql("""
            SELECT
                sh.HRS_ShID AS SourceSacrificeHistoryID,
                sh.TBL_PersonnelID_fk AS SourceID,
                sh.HRS_PbID_fk AS PayBaseID,
                sh.HRS_ShExcuteDate AS ExecuteDate,
                sh.HRS_ShPercent AS SacrificePercent,
                sh.HRS_ShStartDate AS StartDate,
                sh.HRS_ShEndDate AS EndDate,
                sh.HRS_ShDurationDay AS DurationDay,
                sh.HRS_ShNote AS Note,
                pb.HRS_PayBaseName AS SacrificeTitle
            FROM dbo.HRS_SacrificeHistory sh
            LEFT JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID = sh.HRS_PbID_fk
            WHERE sh.HRS_ShActive = 1
              AND sh.TBL_PersonnelID_fk IS NOT NULL
              AND sh.TBL_PersonnelID_fk > 0
        """, source_cnxn)

        if source_df.empty:
            print("No active sacrifice history rows found.")
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
            "SELECT SourceSacrificeHistoryID FROM master.dbo.WarriorMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourceSacrificeHistoryID'].tolist())
            if not mapped_df.empty else set()
        )

        print("Preparing ID generator...")
        warrior_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeWarriorRecord', 0)

        insert_sql = """
            INSERT INTO HCM3.EmployeeWarriorRecord (
                EmployeeWarriorRecordID, EmployeeRef, WarriorGroupCode, StatusCode,
                WarPlace, StartDate, EndDate, Duration, EncouragementGroup,
                ActivityLevelCode, EffectiveDate, Description,
                MartyrRelationCode, MartyrRelationGradeCode, SacrificePercent,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (
                ?, ?, ?, NULL,
                NULL, ?, ?, ?, NULL,
                ?, ?, ?,
                NULL, NULL, ?,
                GETDATE(), 1, GETDATE(), 1
            )
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.WarriorMigrationMapping (
                SourceSacrificeHistoryID, DestEmployeeWarriorRecordID
            ) VALUES (?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        open_ended_ends = 0
        defaulted_effective = 0
        defaulted_group = 0
        today_str = date.today().strftime('%Y-%m-%d')

        print(f"Inserting EmployeeWarriorRecord records ({len(work_df)} candidates)...")
        for _, row in work_df.iterrows():
            source_id = int(row['SourceSacrificeHistoryID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            employee_id = int(row['EmployeeID'])
            pb_id = _as_int_or_none(row['PayBaseID'])

            title = clean_value(row['SacrificeTitle'])
            if title is not None:
                title = normalize_persian(str(title).strip()) or None
            if not title and pb_id and pb_id in name_map:
                title = name_map[pb_id]

            warrior_group, used_default_group = _warrior_group_code(pb_id, parent_map)
            if used_default_group:
                defaulted_group += 1

            activity_level = _activity_level_code(warrior_group, title)

            start_date = _parse_shamsi_date(row['StartDate'])
            end_raw = row['EndDate']
            end_text = (
                str(end_raw).strip().split()[0]
                if end_raw is not None and not (isinstance(end_raw, float) and pd.isna(end_raw))
                else ''
            )
            if end_text == OPEN_END_SHAMSI:
                end_date = None
                open_ended_ends += 1
            else:
                end_date = _parse_shamsi_date(end_raw, treat_open_end_as_null=True)

            effective_date = _parse_shamsi_date(row['ExecuteDate'])
            if effective_date is None:
                effective_date = start_date
            if effective_date is None:
                effective_date = today_str
                defaulted_effective += 1

            duration = _resolve_duration(start_date, end_date, row['DurationDay'])
            sacrifice_pct = _sacrifice_percent(row['SacrificePercent'])
            description = _build_description(title, row['Note'])

            warrior_last_id += 1
            dest_cursor.execute(insert_sql, (
                warrior_last_id,
                employee_id,
                warrior_group,
                start_date,
                end_date,
                duration,
                activity_level,
                effective_date,
                description,
                sacrifice_pct,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_id, warrior_last_id))
            already_mapped.add(source_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeWarriorRecord'",
            (warrior_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Warrior records inserted: {inserted}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Open-ended ends: {open_ended_ends}. "
            f"Defaulted effective date: {defaulted_effective}. "
            f"Defaulted warrior group: {defaulted_group}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Warrior Record step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()

