"""
Step 16: Migrate TBL_Personnel home addresses into GNR3.Address + PartyAddress.

Only personnel with TBL_PersonnelAddressHome.
Name = منزل, Phone = '-', city from first word of address (Type=city RD),
fallback RegionalDivision = کرمانشاه city.
"""
import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_persian_text, normalize_persian

warnings.filterwarnings('ignore', category=UserWarning)

ADDRESS_NAME = 'منزل'
DEFAULT_PHONE = '-'
DEFAULT_CITY_NAME = 'کرمانشاه'
RD_TYPE_CITY = 3


def setup_address_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'AddressMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.AddressMigrationMapping (
                SourcePersonnelID BIGINT PRIMARY KEY,
                DestAddressID BIGINT NOT NULL,
                DestPartyAddressID BIGINT NOT NULL,
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


def _first_word(address_text):
    text = clean_persian_text(address_text)
    if not text:
        return None
    for sep in ('-', '–', '—', '،', ',', '/', '\\', '\t'):
        text = text.replace(sep, ' ')
    parts = [p for p in text.split() if p]
    return parts[0] if parts else None


def _load_city_name_map(dest_cnxn):
    """normalized city name -> smallest RegionalDivisionID (Type=city)."""
    cities = pd.read_sql(f"""
        SELECT RegionalDivisionID, Name
        FROM GNR3.RegionalDivision
        WHERE Type = {RD_TYPE_CITY}
    """, dest_cnxn)
    result = {}
    for _, row in cities.iterrows():
        name = clean_persian_text(row['Name'])
        if not name:
            continue
        rid = int(row['RegionalDivisionID'])
        prev = result.get(name)
        if prev is None or rid < prev:
            result[name] = rid
    return result


def _default_kermanshah_city_id(dest_cursor, city_map):
    default_name = normalize_persian(DEFAULT_CITY_NAME)
    if default_name in city_map:
        return city_map[default_name]
    dest_cursor.execute("""
        SELECT TOP 1 RegionalDivisionID
        FROM GNR3.RegionalDivision
        WHERE Type = ? AND Name = ?
        ORDER BY RegionalDivisionID
    """, (RD_TYPE_CITY, DEFAULT_CITY_NAME))
    row = dest_cursor.fetchone()
    if not row:
        raise RuntimeError("Default city کرمانشاه (Type=city) not found in RegionalDivision.")
    return int(row[0])


def run():
    print("\n--- Running Step 16: Party Address Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_address_mapping_table(dest_cursor)

        print("Loading destination city RegionalDivisions...")
        city_map = _load_city_name_map(dest_cnxn)
        default_city_id = _default_kermanshah_city_id(dest_cursor, city_map)
        print(f"  -> Cities loaded: {len(city_map)}. Default city ID={default_city_id}.")

        print("Fetching personnel with home address + party mapping...")
        party_map_df = pd.read_sql("""
            SELECT SourceID AS SourcePersonnelID, DestPartyID AS PartyID
            FROM master.dbo.PartyMigrationMapping
        """, dest_cnxn)
        source_df = pd.read_sql("""
            SELECT
                TBL_PersonnelID AS SourcePersonnelID,
                TBL_PersonnelAddressHome AS AddressHome,
                TBL_PersonnelPostalCode AS PostalCode
            FROM dbo.TBL_Personnel
            WHERE TBL_PersonnelID > 0
              AND TBL_PersonnelAddressHome IS NOT NULL
              AND LTRIM(RTRIM(TBL_PersonnelAddressHome)) <> N''
              AND LTRIM(RTRIM(TBL_PersonnelAddressHome)) <> N'0'
        """, source_cnxn)

        if source_df.empty or party_map_df.empty:
            print("No personnel with home address found among mapped parties.")
            return

        source_df = pd.merge(source_df, party_map_df, on='SourcePersonnelID', how='inner')
        if source_df.empty:
            print("No personnel with home address found among mapped parties.")
            return

        source_df['Details'] = source_df['AddressHome'].apply(clean_persian_text)
        source_df = source_df[source_df['Details'].notna()].copy()
        if source_df.empty:
            print("No usable home address text after cleaning.")
            return

        mapped_df = pd.read_sql(
            "SELECT SourcePersonnelID FROM master.dbo.AddressMigrationMapping",
            dest_cnxn,
        )
        already_mapped = (
            set(int(x) for x in mapped_df['SourcePersonnelID'].tolist())
            if not mapped_df.empty else set()
        )

        address_last_id = _ensure_table_id(dest_cursor, 'GNR3.Address', 0)
        party_address_last_id = _ensure_table_id(dest_cursor, 'GNR3.PartyAddress', 0)

        insert_address_sql = """
            INSERT INTO GNR3.Address (
                AddressID, Name, Details, ZipCode, Phone, RegionalDivisionRef,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_party_address_sql = """
            INSERT INTO GNR3.PartyAddress (
                PartyAddressID, PartyRef, AddressRef, IsMainAddress
            ) VALUES (?, ?, ?, 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.AddressMigrationMapping (
                SourcePersonnelID, DestAddressID, DestPartyAddressID
            ) VALUES (?, ?, ?)
        """

        inserted = 0
        skipped_already_mapped = 0
        city_matched = 0
        city_defaulted = 0

        print(f"Inserting Address + PartyAddress ({len(source_df)} candidates)...")
        for _, row in source_df.iterrows():
            source_id = int(row['SourcePersonnelID'])
            if source_id in already_mapped:
                skipped_already_mapped += 1
                continue

            details = row['Details'][:2048]
            first = _first_word(details)
            city_id = city_map.get(first) if first else None
            if city_id is None:
                city_id = default_city_id
                city_defaulted += 1
            else:
                city_matched += 1

            zip_code = clean_persian_text(row['PostalCode'])
            if zip_code in (None, '0'):
                zip_code = None
            elif zip_code:
                zip_code = zip_code[:32]

            address_last_id += 1
            party_address_last_id += 1

            dest_cursor.execute(insert_address_sql, (
                address_last_id,
                ADDRESS_NAME,
                details,
                zip_code,
                DEFAULT_PHONE,
                int(city_id),
            ))
            dest_cursor.execute(insert_party_address_sql, (
                party_address_last_id,
                int(row['PartyID']),
                address_last_id,
            ))
            dest_cursor.execute(
                insert_mapping_sql,
                (source_id, address_last_id, party_address_last_id),
            )
            already_mapped.add(source_id)
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'GNR3.Address'",
            (address_last_id,),
        )
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'GNR3.PartyAddress'",
            (party_address_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Addresses inserted: {inserted}. "
            f"City matched: {city_matched}. City defaulted to کرمانشاه: {city_defaulted}. "
            f"Skipped (already mapped): {skipped_already_mapped}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Party Address step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
