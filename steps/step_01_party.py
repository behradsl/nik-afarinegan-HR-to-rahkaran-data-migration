import pandas as pd
import warnings
from db_core import get_connections
from utils.date_helpers import shamsi_to_gregorian
from utils.data_helpers import clean_value

warnings.filterwarnings('ignore', category=UserWarning)


def setup_mapping_table(cursor):
    """Creates the mapping table in the master database if it doesn't exist."""
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'PartyMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.PartyMigrationMapping (
                SourceID INT PRIMARY KEY,
                DestPartyID INT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def run():
    print("\n--- Running Step 1: Base Party Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    setup_mapping_table(dest_cursor)

    print("Fetching Source Data...")
    source_query = """
        SELECT
            TBL_PersonnelID AS SourceID,
            TBL_PersonnelFirstName AS FirstName,
            TBL_PersonnelLastName AS LastName,
            TBL_PersonnelNationaNo AS NationalNo,
            TBL_PersonnelFatherName AS FatherName,
            TBL_PersonnelBirthDate AS BirthDate,
            TBL_PersonnelMobileNo AS Mobile,
            TBL_PersonnelTelNo AS Tel,
            TBL_PersonnelIdentifySerialNo AS IDSerial,
            TBL_PersonnelBirthPLace AS BirthPlace,
            TBL_PersonnelExportPlace AS ExportPlace,
            HRS_SexID_fk AS SexID,
            HRS_MaritalStatusID_fk AS MaritalStatusID
        FROM dbo.TBL_Personnel
    """
    source_df = pd.read_sql(source_query, source_cnxn)

    print("Fetching Destination Deduplication Keys...")
    mapped_ids = set(
        int(x) for x in
        pd.read_sql("SELECT SourceID FROM master.dbo.PartyMigrationMapping", dest_cnxn)['SourceID'].tolist()
    )
    dest_party_df = pd.read_sql("""
        SELECT
            PartyID,
            NationalID,
            Mobile,
            LTRIM(RTRIM(FirstName)) + LTRIM(RTRIM(LastName)) AS FullName
        FROM GNR3.Party
    """, dest_cnxn)

    # Prefer NationalID, then Mobile, then FullName for linking existing parties
    national_to_party = {}
    mobile_to_party = {}
    fullname_to_party = {}
    for _, prow in dest_party_df.iterrows():
        party_id = int(prow['PartyID'])
        nid = prow['NationalID']
        if pd.notna(nid) and str(nid).strip():
            national_to_party.setdefault(str(nid).strip(), party_id)
        mobile = prow['Mobile']
        if pd.notna(mobile) and str(mobile).strip():
            mobile_to_party.setdefault(str(mobile).strip(), party_id)
        full_name = prow['FullName']
        if pd.notna(full_name) and str(full_name).strip():
            fullname_to_party.setdefault(str(full_name).strip(), party_id)

    cities_df = pd.read_sql("SELECT Name, RegionalDivisionID FROM GNR3.RegionalDivision", dest_cnxn)
    city_map = dict(zip(cities_df['Name'], cities_df['RegionalDivisionID']))

    to_insert = []
    to_link = []  # (SourceID, DestPartyID)

    for _, row in source_df.iterrows():
        source_id = int(row['SourceID'])
        if source_id in mapped_ids:
            continue

        existing_party_id = None
        national_no = clean_value(row['NationalNo'])
        if national_no and str(national_no).strip() in national_to_party:
            existing_party_id = national_to_party[str(national_no).strip()]
        else:
            mobile = clean_value(row['Mobile'])
            if mobile and str(mobile).strip() in mobile_to_party:
                existing_party_id = mobile_to_party[str(mobile).strip()]
            else:
                full_name = f"{str(row['FirstName']).strip()}{str(row['LastName']).strip()}"
                if full_name in fullname_to_party:
                    existing_party_id = fullname_to_party[full_name]

        if existing_party_id is not None:
            to_link.append((source_id, existing_party_id))
            mapped_ids.add(source_id)
            continue

        gregorian_birthdate = shamsi_to_gregorian(row['BirthDate'])
        gender = 1 if row['SexID'] == 10001 else (2 if row['SexID'] == 10002 else None)
        marital_status = (
            1 if row['MaritalStatusID'] == 20001
            else (2 if row['MaritalStatusID'] == 20002
                  else (3 if row['MaritalStatusID'] == 20003 else None))
        )

        to_insert.append({
            'SourceID': source_id,
            'FirstName': clean_value(row['FirstName']),
            'LastName': clean_value(row['LastName']),
            'NationalID': national_no,
            'FatherName': clean_value(row['FatherName']),
            'BirthDate': gregorian_birthdate,
            'BirthPlaceRef': city_map.get(row['BirthPlace'], None),
            'IssuancePlaceRef': city_map.get(row['ExportPlace'], None),
            'Mobile': clean_value(row['Mobile']),
            'Tel': clean_value(row['Tel']),
            'IDSerial': clean_value(row['IDSerial']),
            'Gender': gender,
            'MaritalStatus': marital_status,
        })

    if not to_insert and not to_link:
        print("No new Party records to migrate.")
        source_cnxn.close()
        dest_cnxn.close()
        return

    try:
        insert_mapping_sql = """
            INSERT INTO master.dbo.PartyMigrationMapping (SourceID, DestPartyID)
            VALUES (?, ?)
        """

        linked = 0
        for source_id, dest_party_id in to_link:
            dest_cursor.execute(insert_mapping_sql, (source_id, dest_party_id))
            linked += 1

        inserted = 0
        current_last_id = None
        if to_insert:
            print(f"Preparing to insert {len(to_insert)} new Party records...")
            dest_cursor.execute("""
                SELECT LastId
                FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
                WHERE TableName = 'gnr3.party'
            """)
            current_last_id = dest_cursor.fetchone()[0]

            insert_party_sql = """
                INSERT INTO GNR3.Party (
                    PartyID, FirstName, LastName, NationalID, FatherName, BirthDate,
                    BirthPlaceRef, IssuancePlaceRef, Mobile, Tel, IDSerial, Gender, MaritalStatus,
                    CreationDate, Creator, LastModificationDate, LastModifier, Type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1, 0)
            """

            for record in to_insert:
                current_last_id += 1
                new_party_id = current_last_id

                dest_cursor.execute(insert_party_sql, (
                    new_party_id, record['FirstName'], record['LastName'], record['NationalID'],
                    record['FatherName'], record['BirthDate'], record['BirthPlaceRef'],
                    record['IssuancePlaceRef'], record['Mobile'], record['Tel'],
                    record['IDSerial'], record['Gender'], record['MaritalStatus'],
                ))
                dest_cursor.execute(insert_mapping_sql, (record['SourceID'], new_party_id))
                inserted += 1

            dest_cursor.execute("""
                UPDATE SYS3.tableIdGen
                SET LastId = ?
                WHERE TableName = 'gnr3.party'
            """, (current_last_id,))

        dest_cnxn.commit()
        print(
            f"Success! Party inserted: {inserted}. "
            f"Linked existing parties: {linked}."
            + (f" New Party LastId is {current_last_id}." if current_last_id is not None else "")
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
