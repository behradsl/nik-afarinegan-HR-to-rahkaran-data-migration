import pandas as pd
import warnings
from db_core import get_connections
from utils.date_helpers import shamsi_to_gregorian

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

def clean_value(val):
    """Converts '0', 0, empty strings, and NaNs to Python None (which becomes SQL NULL)."""
    if pd.isna(val): 
        return None
    val_str = str(val).strip()
    if val_str in ('', '0', '0.0', 'None'): 
        return None
    return val

def run():
    print("\n--- Running Step 1: Base Party Migration ---")
    
    source_cnxn, dest_cnxn = get_connections()
    source_cursor = source_cnxn.cursor()
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
    mapped_ids = pd.read_sql("SELECT SourceID FROM master.dbo.PartyMigrationMapping", dest_cnxn)['SourceID'].tolist()
    dest_party_df = pd.read_sql("SELECT NationalID, Mobile, LTRIM(RTRIM(FirstName)) + LTRIM(RTRIM(LastName)) AS FullName FROM GNR3.Party", dest_cnxn)
    
    existing_national_ids = set(dest_party_df['NationalID'].dropna())
    existing_mobiles = set(dest_party_df['Mobile'].dropna())
    existing_fullnames = set(dest_party_df['FullName'].dropna())

    cities_df = pd.read_sql("SELECT Name, RegionalDivisionID FROM GNR3.RegionalDivision", dest_cnxn)
    city_map = dict(zip(cities_df['Name'], cities_df['RegionalDivisionID']))

    valid_records = []
    for index, row in source_df.iterrows():
        if row['SourceID'] in mapped_ids: continue
        if row['NationalNo'] and row['NationalNo'] in existing_national_ids: continue
        if row['Mobile'] and row['Mobile'] in existing_mobiles: continue
        
        full_name = f"{str(row['FirstName']).strip()}{str(row['LastName']).strip()}"
        if full_name in existing_fullnames: continue

        gregorian_birthdate = shamsi_to_gregorian(row['BirthDate'])
        gender = 1 if row['SexID'] == 1001 else (2 if row['SexID'] == 1002 else None)
        marital_status = 1 if row['MaritalStatusID'] == 20001 else (2 if row['MaritalStatusID'] == 20002 else None)
        
        valid_records.append({
            'SourceID': row['SourceID'],
            'FirstName': clean_value(row['FirstName']),
            'LastName': clean_value(row['LastName']),
            'NationalID': clean_value(row['NationalNo']),
            'FatherName': clean_value(row['FatherName']),
            'BirthDate': gregorian_birthdate,
            'BirthPlaceRef': city_map.get(row['BirthPlace'], None),
            'IssuancePlaceRef': city_map.get(row['ExportPlace'], None),
            'Mobile': clean_value(row['Mobile']),
            'Tel': clean_value(row['Tel']),
            'IDSerial': clean_value(row['IDSerial']),
            'Gender': gender,
            'MaritalStatus': marital_status
        })

    if not valid_records:
        print("No new Party records to migrate.")
        return

    try:
        print(f"Preparing to insert {len(valid_records)} records...")
        
        dest_cursor.execute("""
            SELECT LastId 
            FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) 
            WHERE TableName = 'gnr3.party'
        """)
        current_last_id = dest_cursor.fetchone()[0]
        
        # FIXED: Added Type to the columns and 0 to the VALUES
        insert_party_sql = """
            INSERT INTO GNR3.Party (
                PartyID, FirstName, LastName, NationalID, FatherName, BirthDate, 
                BirthPlaceRef, IssuancePlaceRef, Mobile, Tel, IDSerial, Gender, MaritalStatus,
                CreationDate, Creator, LastModificationDate, LastModifier, Type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1, 0)
        """
        
        insert_mapping_sql = """
            INSERT INTO master.dbo.PartyMigrationMapping (SourceID, DestPartyID)
            VALUES (?, ?)
        """

        for record in valid_records:
            current_last_id += 1
            new_party_id = current_last_id
            
            dest_cursor.execute(insert_party_sql, (
                new_party_id, record['FirstName'], record['LastName'], record['NationalID'], 
                record['FatherName'], record['BirthDate'], record['BirthPlaceRef'], 
                record['IssuancePlaceRef'], record['Mobile'], record['Tel'], 
                record['IDSerial'], record['Gender'], record['MaritalStatus']
            ))
            
            dest_cursor.execute(insert_mapping_sql, (record['SourceID'], new_party_id))

        dest_cursor.execute("""
            UPDATE SYS3.tableIdGen 
            SET LastId = ? 
            WHERE TableName = 'gnr3.party'
        """, (current_last_id,))

        dest_cnxn.commit()
        print(f"Success! Migrated {len(valid_records)} records. New Party LastId is {current_last_id}.")

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during insertion. Transaction rolled back. Error: {e}")
        # FIXED: Re-raise the exception so main.py knows the step actually failed
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()