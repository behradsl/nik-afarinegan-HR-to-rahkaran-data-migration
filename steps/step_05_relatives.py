import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, clean_persian_text
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_lookup_codes, sync_lookup
from utils.hcm_extra_settings import ensure_hcm_extra_fields

warnings.filterwarnings('ignore', category=UserWarning)

ALLOWED_RELATIONS = {30002, 30003, 30004, 30005, 30006}
SPOUSE_RELATION = 30002
MASS_REGISTER_DATE = '1391/11/30'
DEFAULT_BIRTH = '1900-01-01'
DEFAULT_NATIONAL_ID = '0'
DEFAULT_FIRST_NAME = '-'
DEFAULT_ORG_CODE = 'MIG-INS-DEFAULT'
DEFAULT_ORG_TITLE = 'سازمان بیمه پیش‌فرض مهاجرت'

RELATION_MAP = {
    30003: 7,  # پسر
    30004: 8,  # دختر
    30005: 1,  # پدر
    30006: 2,  # مادر
}

# HRS_SponserStatusID_fk (PayBase parent 15) → dest relative status codes
# EducationState: 1=محصل, 2=دانشجو
SPONSOR_EDUCATION_STATE = {
    150011: 1,  # محصل
    150005: 2,  # دانشجو
}
# PhysicalState: 1=سالم, 2=معلول
SPONSOR_PHYSICAL_STATE = {
    150016: 2,  # معلول/ازکارافتاده کلی
}
# MaritalStatus: 1=مجرد, 2=متأهل, 3=معیل
SPONSOR_MARITAL_STATUS = {
    150013: 1,  # مجرد
    150006: 1,  # دختر ازدواج نکرده
    150007: 1,  # دختر مطلقه
    150017: 1,  # طلاق
    150008: 2,  # متاهل
}

# Insurance statuses that mean "no coverage" — skip insurance row
INSURANCE_STATUS_SKIP = {0, 710008}  # پایه / ندارد

EDUCATION_STATE_LOOKUP = {1: 'محصل', 2: 'دانشجو'}
PHYSICAL_STATE_LOOKUP = {1: 'سالم', 2: 'معلول'}


def _parse_shamsi_date(raw, *, reject_mass_register=False):
    """Parse Shamsi date strings; reject common junk placeholders."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
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
        ('EmployeDate', False),  # fallback: employee hire date when no marriage date
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


def _as_int(val, default=None):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _status_codes_from_sponsor(sponsor_status_id):
    """Return (education_state, physical_state, marital_status) from SponserStatus.

    Unset physical state defaults to 1 (سالم).
    """
    sid = _as_int(sponsor_status_id)
    education = SPONSOR_EDUCATION_STATE.get(sid)
    physical = SPONSOR_PHYSICAL_STATE.get(sid)
    if physical is None:
        physical = 1  # سالم
    marital = SPONSOR_MARITAL_STATUS.get(sid)
    return education, physical, marital


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


def setup_relative_insurance_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'RelativeInsuranceMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.RelativeInsuranceMigrationMapping (
                SourceSponsorID INT PRIMARY KEY,
                DestEmployeeRelativeInsuranceID BIGINT NOT NULL,
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
    """Return cleaned Persian text or default; second value True if defaulted."""
    text = clean_persian_text(val)
    if text is None:
        return default, True
    return text, False


def ensure_default_insurance_organization(dest_cnxn, dest_cursor):
    """
    EmployeeRelativeInsurance.OrganizationRef is NOT NULL.
    Use existing org or insert a migration default (OrganizationType=1 تامین اجتماعی).
    """
    existing = pd.read_sql(
        "SELECT TOP 1 OrganizationID FROM HCM3.Organization ORDER BY OrganizationID",
        dest_cnxn,
    )
    if not existing.empty:
        return int(existing.iloc[0]['OrganizationID'])

    print("  -> Creating default insurance Organization...")
    last_id = _ensure_table_id(dest_cursor, 'HCM3.Organization', 0)
    last_id += 1
    dest_cursor.execute("""
        INSERT INTO HCM3.Organization (
            OrganizationID, Code, Title, TypeCode,
            MandatoryEmployeeRelatedCode, Nature,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, 1, 0, 1, GETDATE(), 1, GETDATE(), 1)
    """, (last_id, DEFAULT_ORG_CODE, DEFAULT_ORG_TITLE))
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Organization'",
        (last_id,),
    )
    return last_id


def _should_insert_insurance(row):
    """Insert insurance when there is a create date or a meaningful insurance status."""
    status_id = _as_int(row.get('InsuranceStatusID'), 0) or 0
    if status_id in INSURANCE_STATUS_SKIP:
        start = _parse_shamsi_date(row.get('InsuranceCreateDate'))
        return start is not None
    start = _parse_shamsi_date(row.get('InsuranceCreateDate'))
    if start is not None:
        return True
    # Status says covered but no date — still insert with fallback start
    return status_id not in INSURANCE_STATUS_SKIP and status_id > 0


def _insurance_number(row):
    book = row.get('BookNo')
    if book is None or (isinstance(book, float) and pd.isna(book)):
        return None
    try:
        num = int(float(book))
    except (TypeError, ValueError):
        text = str(book).strip()
        return text[:50] if text and text not in ('0', 'None') else None
    if num <= 0:
        return None
    return str(num)[:50]


def _is_surety(sponsor_ship_status):
    """Map HRS_SponserShipStatus (تحت تکفل flag) → IsSurety."""
    try:
        return 1 if int(sponsor_ship_status) == 1 else 0
    except (TypeError, ValueError):
        return 0


def run():
    print("\n--- Running Step 5: Employee Relatives Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_relative_mapping_table(dest_cursor)
        setup_relative_insurance_mapping_table(dest_cursor)

        print("Ensuring EducationState / PhysicalState lookups...")
        ensure_lookup_codes(dest_cnxn, dest_cursor, 'EducationState', EDUCATION_STATE_LOOKUP)
        ensure_lookup_codes(dest_cnxn, dest_cursor, 'PhysicalState', PHYSICAL_STATE_LOOKUP)

        print("Ensuring default insurance Organization...")
        organization_ref = ensure_default_insurance_organization(dest_cnxn, dest_cursor)

        print("Fetching Source Sponsorship / Relatives...")
        source_df = pd.read_sql("""
            SELECT
                ss.HRS_SSID AS SourceSponsorID,
                ss.TBL_PersonnelID_fk AS SourceID,
                ss.HRS_SponserRelatedID_fk AS SourceRelatedID,
                ss.HRS_SponserStatusID_fk AS SponsorStatusID,
                ss.HRS_CreateStatusID_fk AS CreateStatusID,
                pb.HRS_PayBaseName AS CreateStatusName,
                ss.HRS_SponserShipStatus AS SponsorShipStatus,
                ss.HRS_InsuranceStatusID_fk AS InsuranceStatusID,
                ss.HRS_SsFirstName AS FirstName,
                ss.HRS_SsLastName AS LastName,
                ss.HRS_SsFatherName AS FatherName,
                ss.HRS_SsNationalCode AS NationalID,
                ss.HRS_SsBirthDate AS BirthDate,
                ss.HRS_SsIdentifyNo AS IDNumber,
                ss.HRS_SsBookNo AS BookNo,
                ss.HRS_SsWelfareCreateDate AS WelfareCreateDate,
                ss.HRS_InsuranceCreateDate AS InsuranceCreateDate,
                ss.HRS_SsRegisterDate AS RegisterDate,
                ss.HRS_SsWelfareDeleteDate AS WelfareDeleteDate,
                ss.HRS_InsuranceDeleteDate AS InsuranceDeleteDate,
                ss.HRS_SsDeathDate AS DeathDate,
                p.TBL_PersonnelEmployeDate AS EmployeDate
            FROM dbo.HRS_SponsorShip ss
            LEFT JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID = ss.HRS_CreateStatusID_fk
            LEFT JOIN dbo.TBL_Personnel p ON p.TBL_PersonnelID = ss.TBL_PersonnelID_fk
            WHERE ss.TBL_PersonnelID_fk IS NOT NULL
              AND ss.TBL_PersonnelID_fk > 0
              AND ss.HRS_SponserRelatedID_fk IN (30002, 30003, 30004, 30005, 30006)
        """, source_cnxn)

        if source_df.empty:
            print("No relative rows found.")
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

        print("Syncing EmployeeRelativeExtra1 from cause of being relative (علت ایجاد تحت تکفل)...")
        ensure_hcm_extra_fields(dest_cursor, ('EmployeeRelativeExtra1',))
        work_df['CreateStatusClean'] = work_df['CreateStatusName'].apply(clean_persian_text)
        cause_names = [
            n for n in work_df['CreateStatusClean'].dropna().unique().tolist() if n
        ]
        catalog_df = pd.read_sql("""
            SELECT HRS_PayBaseName
            FROM dbo.HRS_PayBase
            WHERE HRS_PayBaseParentID_fk = 77
        """, source_cnxn)
        for raw in catalog_df['HRS_PayBaseName'].dropna().unique():
            name = clean_persian_text(raw)
            if name and name not in cause_names:
                cause_names.append(name)

        name_to_extra1 = sync_lookup(
            dest_cnxn, dest_cursor, 'EmployeeRelativeExtra1', cause_names
        )
        id_to_extra1 = {}
        for _, row in work_df[['CreateStatusID', 'CreateStatusClean']].drop_duplicates().iterrows():
            cid = _as_int(row['CreateStatusID'])
            name = row['CreateStatusClean']
            if cid is not None and name and name in name_to_extra1:
                id_to_extra1[cid] = name_to_extra1[name]

        mapped_df = pd.read_sql("""
            SELECT SourceSponsorID, DestEmployeeRelativeID
            FROM master.dbo.RelativeMigrationMapping
        """, dest_cnxn)
        already_mapped = {}
        if not mapped_df.empty:
            already_mapped = {
                int(row['SourceSponsorID']): int(row['DestEmployeeRelativeID'])
                for _, row in mapped_df.iterrows()
            }

        insured_mapped_df = pd.read_sql("""
            SELECT SourceSponsorID, DestEmployeeRelativeInsuranceID
            FROM master.dbo.RelativeInsuranceMigrationMapping
        """, dest_cnxn)
        already_insured = {}
        if not insured_mapped_df.empty:
            already_insured = {
                int(row['SourceSponsorID']): int(row['DestEmployeeRelativeInsuranceID'])
                for _, row in insured_mapped_df.iterrows()
            }

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
        insurance_last_id = _ensure_table_id(dest_cursor, 'HCM3.EmployeeRelativeInsurance', 0)

        insert_relative_sql = """
            INSERT INTO HCM3.EmployeeRelative (
                EmployeeRelativeID, EmployeeRef, FirstName, LastName, FatherName,
                RelationCode, AllegianceCode, NationalID, IDNumber, BirthDate,
                DegreeCode, EducationStateCode, PhysicalStateCode, MaritalStatusCode,
                EmployeeRelativeExtra1Code,
                IsFourthChild, IncludeInSonshipPay, EffectiveDate, RelativeType,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?, ?, ?, 0, 0, ?, 1, GETDATE(), 1, GETDATE(), 1)
        """
        update_relative_status_sql = """
            UPDATE HCM3.EmployeeRelative
            SET EducationStateCode = ?,
                PhysicalStateCode = ?,
                MaritalStatusCode = ?,
                EmployeeRelativeExtra1Code = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeRelativeID = ?
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
        insert_insurance_sql = """
            INSERT INTO HCM3.EmployeeRelativeInsurance (
                EmployeeRelativeInsuranceID, EmployeeRelativeRef, OrganizationRef,
                StartDate, EndDate, InsuranceNumber, IsSurety,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_insurance_mapping_sql = """
            INSERT INTO master.dbo.RelativeInsuranceMigrationMapping (
                SourceSponsorID, DestEmployeeRelativeInsuranceID
            ) VALUES (?, ?)
        """
        update_insurance_surety_sql = """
            UPDATE HCM3.EmployeeRelativeInsurance
            SET IsSurety = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE EmployeeRelativeInsuranceID = ?
        """
        update_party_sql = """
            UPDATE GNR3.Party
            SET MaritalStatus = ?,
                LastModificationDate = GETDATE(),
                LastModifier = 1
            WHERE PartyID = ?
        """

        relatives_inserted = 0
        relatives_status_updated = 0
        insurance_inserted = 0
        insurance_surety_updated = 0
        marriages_inserted = 0
        defaulted_fields = 0
        skipped_already_mapped = 0
        skipped_marriage_no_date = 0
        skipped_insurance = 0

        employees_touched = {
            (int(r['EmployeeID']), int(r['PartyRef']))
            for _, r in work_df.iterrows()
        }
        newly_spouse_employees = set()
        spouse_events_by_employee = {}

        def maybe_insert_insurance(source_sponsor_id, dest_relative_id, row):
            nonlocal insurance_last_id, insurance_inserted, insurance_surety_updated, skipped_insurance
            is_surety = _is_surety(row.get('SponsorShipStatus'))

            if source_sponsor_id in already_insured:
                dest_cursor.execute(
                    update_insurance_surety_sql,
                    (is_surety, already_insured[source_sponsor_id]),
                )
                insurance_surety_updated += 1
                return
            if not _should_insert_insurance(row):
                skipped_insurance += 1
                return

            start = _parse_shamsi_date(row.get('InsuranceCreateDate'))
            if start is None:
                start = _parse_shamsi_date(row.get('RegisterDate'), reject_mass_register=True)
            if start is None:
                start = _parse_shamsi_date(row.get('BirthDate')) or DEFAULT_BIRTH

            end = _parse_shamsi_date(row.get('InsuranceDeleteDate'))
            number = _insurance_number(row)

            insurance_last_id += 1
            dest_cursor.execute(insert_insurance_sql, (
                insurance_last_id,
                dest_relative_id,
                organization_ref,
                start,
                end,
                number,
                is_surety,
            ))
            dest_cursor.execute(insert_insurance_mapping_sql, (source_sponsor_id, insurance_last_id))
            already_insured[source_sponsor_id] = insurance_last_id
            insurance_inserted += 1

        print("Inserting/updating EmployeeRelative records...")
        for _, row in work_df.iterrows():
            source_sponsor_id = int(row['SourceSponsorID'])
            education_code, physical_code, marital_code = _status_codes_from_sponsor(
                row['SponsorStatusID']
            )
            create_status_id = _as_int(row.get('CreateStatusID'))
            extra1_code = id_to_extra1.get(create_status_id) if create_status_id is not None else None
            if extra1_code is None:
                cause_name = row.get('CreateStatusClean')
                if cause_name:
                    extra1_code = name_to_extra1.get(cause_name)

            if source_sponsor_id in already_mapped:
                dest_rel_id = already_mapped[source_sponsor_id]
                dest_cursor.execute(
                    update_relative_status_sql,
                    (education_code, physical_code, marital_code, extra1_code, dest_rel_id),
                )
                relatives_status_updated += 1
                maybe_insert_insurance(source_sponsor_id, dest_rel_id, row)
                skipped_already_mapped += 1
                continue

            employee_id = int(row['EmployeeID'])

            first_name, d1 = _required_text(row['FirstName'], DEFAULT_FIRST_NAME)
            national_id, d2 = _required_text(row['NationalID'], DEFAULT_NATIONAL_ID)
            national_id = str(national_id)[:20]
            if d1 or d2:
                defaulted_fields += 1

            last_name = clean_persian_text(row['LastName'])
            father_name = clean_persian_text(row['FatherName'])

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
                education_code,
                physical_code,
                marital_code,
                extra1_code,
                effective_date,
            ))
            dest_cursor.execute(insert_mapping_sql, (source_sponsor_id, relative_last_id))
            already_mapped[source_sponsor_id] = relative_last_id
            relatives_inserted += 1

            maybe_insert_insurance(source_sponsor_id, relative_last_id, row)

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
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmployeeRelativeInsurance'",
            (insurance_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Relatives inserted: {relatives_inserted}. "
            f"Status updated: {relatives_status_updated}. "
            f"Insurance inserted: {insurance_inserted}. "
            f"IsSurety updated: {insurance_surety_updated}. "
            f"Marriages inserted: {marriages_inserted}. "
            f"Party marital updates: {party_updates}. "
            f"Skipped (no employee): {skipped_no_employee}. "
            f"Skipped (already mapped): {skipped_already_mapped}. "
            f"Skipped insurance: {skipped_insurance}. "
            f"Defaulted required fields: {defaulted_fields}. "
            f"Skipped marriage (no date): {skipped_marriage_no_date}. "
            f"Multi-spouse people: {multi_spouse_people}. "
            f"OrganizationRef used: {organization_ref}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Relatives step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
