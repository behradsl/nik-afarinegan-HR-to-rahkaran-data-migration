import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, normalize_persian
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

ALLOWED_RELATIONS = {30002, 30003, 30004, 30005, 30006}
SPOUSE_RELATION = 30002
MASS_REGISTER_DATE = '1391/11/30'
DEFAULT_BIRTH = '1900-01-01'
DEFAULT_NATIONAL_ID = '0'
DEFAULT_FIRST_NAME = '-'

RELATION_MAP = {
    30003: 7,  # پسر
    30004: 8,  # دختر
    30005: 1,  # پدر
    30006: 2,  # مادر
}


def _parse_shamsi_date(raw, *, reject_mass_register=False):
    """Parse Shamsi date strings; reject common junk placeholders."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    # RegisterDate may include time: "1391/11/30 8:40:29"
    date_part = text.split()[0]
    if date_part in ('', '0', '____/__/__', '/  /', '//', '0/0/0'):
        return None
    if reject_mass_register and date_part == MASS_REGISTER_DATE:
        return None
    if '_' in date_part or date_part.count('/') != 2:
        return None
    return shamsi_to_gregorian(date_part)


def _resolve_marriage_start(row):
    for col, reject_mass in (
        ('WelfareCreateDate', False),
        ('InsuranceCreateDate', False),
        ('RegisterDate', True),
    ):
        parsed = _parse_shamsi_date(row.get(col), reject_mass_register=reject_mass)
        if parsed:
            return parsed
    return None


def _resolve_marriage_end(row):
    for col in ('WelfareDeleteDate', 'InsuranceDeleteDate', 'DeathDate'):
        parsed = _parse_shamsi_date(row.get(col))
        if parsed:
            return parsed
    return None


def _spouse_relation_code(gender):
    try:
        g = int(gender)
    except (TypeError, ValueError):
        return 3
    if g == 2:  # female employee -> husband
        return 4
    return 3  # male / unknown -> wife


def _relation_code(source_related_id, gender):
    source_related_id = int(source_related_id)
    if source_related_id == SPOUSE_RELATION:
        return _spouse_relation_code(gender)
    return RELATION_MAP[source_related_id]


def setup_relative_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'RelativeMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.RelativeMigrationMapping (
                SourceSponsorID INT PRIMARY KEY,
                DestEmployeeRelativeID BIGINT NOT NULL,
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


def _required_text(val, default):
    cleaned = clean_value(val)
    if cleaned is None:
        return default, True
    text = normalize_persian(str(cleaned).strip()) if isinstance(cleaned, str) else str(cleaned).strip()
    if not text:
        return default, True
    return text, False


def run():
    print("\n--- Running Step 5: Employee Relatives Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_relative_mapping_table(dest_cursor)

        print("Fetching Source Sponsorship / Relatives...")
        source_df = pd.read_sql("""
            SELECT
                ss.HRS_SSID AS SourceSponsorID,
                ss.TBL_PersonnelID_fk AS SourceID,
                ss.HRS_SponserRelatedID_fk AS SourceRelatedID,
                ss.HRS_SsFirstName AS FirstName,
                ss.HRS_SsLastName AS LastName,
                ss.HRS_SsFatherName AS FatherName,
                ss.HRS_SsNationalCode AS NationalID,
                ss.HRS_SsBirthDate AS BirthDate,
                ss.HRS_SsIdentifyNo AS IDNumber,
                ss.HRS_SsWelfareCreateDate AS WelfareCreateDate,
                ss.HRS_InsuranceCreateDate AS InsuranceCreateDate,
                ss.HRS_SsRegisterDate AS RegisterDate,
                ss.HRS_SsWelfareDeleteDate AS WelfareDeleteDate,
                ss.HRS_InsuranceDeleteDate AS InsuranceDeleteDate,
                ss.HRS_SsDeathDate AS DeathDate
            FROM dbo.HRS_SponsorShip ss
            WHERE ss.HRS_SsActive = 1
              AND ss.TBL_PersonnelID_fk IS NOT NULL
              AND ss.TBL_PersonnelID_fk > 0
              AND ss.HRS_SponserRelatedID_fk IN (30002, 30003, 30004, 30005, 30006)
        """, source_cnxn)

        if source_df.empty:
            print("No active relative rows found.")
            return

        multi_spouse_people = int(
            (
                source_df[source_df['SourceRelatedID'] == SPOUSE_RELATION]
                .groupby('SourceID')
                .size() > 1
            ).sum()
        )

        print("Mapping Source to Rahkaran Employees...")
        emp_map_df = pd.read_sql("""
            SELECT
                m.SourceID,
                e.EmployeeID,
                e.PartyRef,
                p.Gender,
                p.BirthDate AS PartyBirthDate,
                p.MaritalStatus AS PartyMaritalStatus
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
            JOIN GNR3.Party p ON p.PartyID = e.PartyRef
        """, dest_cnxn)

        merged_df = pd.merge(source_df, emp_map_df, on='SourceID', how='left')
        skipped_no_employee = int(merged_df['EmployeeID'].isna().sum())
        work_df = merged_df[merged_df['EmployeeID'].notna()].copy()

        if work_df.empty:
            print(f"No matching employees found. Skipped (no employee): {skipped_no_employee}.")
            return

        mapped_df = pd.read_sql(
            "SELECT SourceSponsorID FROM master.dbo.RelativeMigrationMapping",
            dest_cnxn,
        )
        already_mapped = set(mapped_df['SourceSponsorID'].tolist()) if not mapped_df.empty else set()

        existing_marriage_df = pd.read_sql("""
            SELECT EmployeeRef, StatusCode, EffectiveDate
            FROM HCM3.EmployeeMarriage
        """, dest_cnxn)
        existing_marriage_keys = set()
        for _, row in existing_marriage_df.iterrows():
            eff = row['EffectiveDate']
            if pd.isna(eff):
                continue
            eff_str = pd.to_datetime(eff).strftime('%Y-%m-%d')
            existing_marriage_keys.add((int(row['EmployeeRef']), int(row['StatusCode']), eff_str))

        print("Preparing ID generators...")
        relative_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeRelative', 0)
        marriage_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeMarriage', 0)

        insert_relative_sql = """
            INSERT INTO HCM3.EmployeeRelative (
                EmployeeRelativeID, EmployeeRef, FirstName, LastName, FatherName,
                RelationCode, AllegianceCode, NationalID, IDNumber, BirthDate,
                IsFourthChild, IncludeInSonshipPay, EffectiveDate, RelativeType,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 0, ?, 1, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.RelativeMigrationMapping (SourceSponsorID, DestEmployeeRelativeID)
            VALUES (?, ?)
        """
        insert_marriage_sql = """
            INSERT INTO HCM3.EmployeeMarriage (
                EmployeeMarriageID, EmployeeRef, StatusCode, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        update_party_sql = """
            UPDATE GNR3.Party
            SET MaritalStatus = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE PartyID = ?
        """

        relatives_inserted = 0
        marriages_inserted = 0
        defaulted_fields = 0
        skipped_already_mapped = 0
        skipped_marriage_no_date = 0

        employees_touched = {
            (int(r['EmployeeID']), int(r['PartyRef']))
            for _, r in work_df.iterrows()
        }
        newly_spouse_employees = set()  # got a new spouse relative this run
        spouse_events_by_employee = {}  # EmployeeID -> list of (start, end)

        print("Inserting EmployeeRelative records...")
        for _, row in work_df.iterrows():
            source_sponsor_id = int(row['SourceSponsorID'])
            if source_sponsor_id in already_mapped:
                skipped_already_mapped += 1
                continue

            employee_id = int(row['EmployeeID'])

            first_name, d1 = _required_text(row['FirstName'], DEFAULT_FIRST_NAME)
            national_id, d2 = _required_text(row['NationalID'], DEFAULT_NATIONAL_ID)
            # NationalID column is varchar(20)
            national_id = str(national_id)[:20]
            if d1 or d2:
                defaulted_fields += 1

            last_name = clean_value(row['LastName'])
            if last_name is not None:
                last_name = normalize_persian(str(last_name).strip()) or None

            father_name = clean_value(row['FatherName'])
            if father_name is not None:
                father_name = normalize_persian(str(father_name).strip()) or None

            id_number = clean_value(row['IDNumber'])
            if id_number is not None:
                id_number = str(id_number).strip()[:20] or None

            birth_date = _parse_shamsi_date(row['BirthDate'])
            if birth_date is None:
                birth_date = DEFAULT_BIRTH
                defaulted_fields += 1

            relation_code = _relation_code(row['SourceRelatedID'], row['Gender'])
            effective_date = birth_date

            relative_last_id += 1
            dest_cursor.execute(insert_relative_sql, (
                relative_last_id,
                employee_id,
                first_name,
                last_name,
                father_name,
                relation_code,
                national_id,
                id_number,
                birth_date,
                effective_date,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_sponsor_id, relative_last_id))
            relatives_inserted += 1
            already_mapped.add(source_sponsor_id)

            if int(row['SourceRelatedID']) == SPOUSE_RELATION:
                newly_spouse_employees.add(employee_id)
                start = _resolve_marriage_start(row)
                end = _resolve_marriage_end(row)
                if start is None:
                    skipped_marriage_no_date += 1
                else:
                    spouse_events_by_employee.setdefault(employee_id, []).append((start, end))

        def add_marriage(employee_id, status_code, effective_date):
            nonlocal marriage_last_id, marriages_inserted
            key = (employee_id, status_code, effective_date)
            if key in existing_marriage_keys:
                return
            marriage_last_id += 1
            dest_cursor.execute(insert_marriage_sql, (
                marriage_last_id, employee_id, status_code, effective_date
            ))
            existing_marriage_keys.add(key)
            marriages_inserted += 1

        print("Building EmployeeMarriage history for newly inserted spouses...")
        party_birth_by_employee = {
            int(r['EmployeeID']): (
                pd.to_datetime(r['PartyBirthDate']).strftime('%Y-%m-%d')
                if pd.notna(r['PartyBirthDate']) else DEFAULT_BIRTH
            )
            for _, r in work_df.drop_duplicates('EmployeeID').iterrows()
            if pd.notna(r['EmployeeID'])
        }

        party_ref_by_employee = {
            int(emp_id): int(party_ref)
            for emp_id, party_ref in employees_touched
        }

        for employee_id in newly_spouse_employees:
            birth = party_birth_by_employee.get(employee_id, DEFAULT_BIRTH)
            add_marriage(employee_id, 1, birth)

            events = sorted(spouse_events_by_employee.get(employee_id, []), key=lambda x: x[0])
            for start, end in events:
                add_marriage(employee_id, 2, start)
                if end and end > start:
                    add_marriage(employee_id, 1, end)

        print("Updating Party.MaritalStatus for newly married employees...")
        party_updates = 0
        for employee_id in newly_spouse_employees:
            party_ref = party_ref_by_employee.get(employee_id)
            if party_ref is None:
                continue
            dest_cursor.execute(update_party_sql, (2, party_ref))
            party_updates += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeRelative'",
            (relative_last_id,),
        )
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeMarriage'",
            (marriage_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Relatives inserted: {relatives_inserted}. "
            f"Marriages inserted: {marriages_inserted}. "
            f"Party marital updates: {party_updates}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Defaulted required fields: {defaulted_fields}. "
            f"Skipped marriage (no date): {skipped_marriage_no_date}. "
            f"Multi-spouse people: {multi_spouse_people}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Relatives step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
