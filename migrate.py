import json
import pyodbc
import jdatetime
import pandas as pd
from datetime import datetime

# --- 1. CONFIGURATION & SETUP ---
with open('config.json', 'r') as f:
    config = json.load(f)

source_cnxn = pyodbc.connect(config['source_conn'])
dest_cnxn = pyodbc.connect(config['dest_conn'], autocommit=False) # Manage transactions manually

# --- 2. HELPER FUNCTIONS ---
def shamsi_to_gregorian(shamsi_str):
    if not shamsi_str or str(shamsi_str).strip() == '':
        return None
    try:
        parts = str(shamsi_str).split('/')
        if len(parts) == 3:
            # jdatetime converts 1388/02/05 to standard datetime
            jdate = jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
            return jdate.togregorian().strftime('%Y-%m-%d')
    except Exception as e:
        print(f"Date conversion error for {shamsi_str}: {e}")
    return None

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

# --- 3. MAIN MIGRATION LOGIC ---
def migrate_personnel():
    dest_cursor = dest_cnxn.cursor()
    source_cursor = source_cnxn.cursor()
    
    setup_mapping_table(dest_cursor)

    print("Fetching Source Data...")
    # Add a pseudo SourceID (or use actual PK if TBL_Personnel has one)
    source_query = """
        SELECT 
            ID AS SourceID, -- Assuming there is a primary key in source
            TBL_PersonnelFirstName AS FirstName,
            TBL_PersonnelLastName AS LastName,
            TBL_PersonnelNationalNo AS NationalNo,
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
    # Fetch existing mappings and destination records to prevent duplicates
    mapped_ids = pd.read_sql("SELECT SourceID FROM master.dbo.PartyMigrationMapping", dest_cnxn)['SourceID'].tolist()
    dest_party_df = pd.read_sql("SELECT NationalID, Mobile, LTRIM(RTRIM(FirstName)) + LTRIM(RTRIM(LastName)) AS FullName FROM GNR3.Party", dest_cnxn)
    
    existing_national_ids = set(dest_party_df['NationalID'].dropna())
    existing_mobiles = set(dest_party_df['Mobile'].dropna())
    existing_fullnames = set(dest_party_df['FullName'].dropna())

    print("Fetching Regional Divisions...")
    cities_df = pd.read_sql("SELECT Name, RegionalDivisionID FROM GNR3.RegionalDivision", dest_cnxn)
    city_map = dict(zip(cities_df['Name'], cities_df['RegionalDivisionID']))

    valid_records = []
    
    # --- 4. TRANSFORMATION & DEDUPLICATION ---
    for index, row in source_df.iterrows():
        # A. Deduplication Checks
        if row['SourceID'] in mapped_ids: continue
        if row['NationalNo'] and row['NationalNo'] in existing_national_ids: continue
        if row['Mobile'] and row['Mobile'] in existing_mobiles: continue
        
        full_name = f"{str(row['FirstName']).strip()}{str(row['LastName']).strip()}"
        if full_name in existing_fullnames: continue

        # B. Data Transformation
        gregorian_birthdate = shamsi_to_gregorian(row['BirthDate'])
        gender = 1 if row['SexID'] == 1001 else (2 if row['SexID'] == 1002 else None)
        marital_status = 1 if row['MaritalStatusID'] == 20001 else (2 if row['MaritalStatusID'] == 20002 else None)
        
        birth_place_ref = city_map.get(row['BirthPlace'], None)
        issuance_place_ref = city_map.get(row['ExportPlace'], None)

        valid_records.append({
            'SourceID': row['SourceID'],
            'FirstName': row['FirstName'],
            'LastName': row['LastName'],
            'NationalID': row['NationalNo'],
            'FatherName': row['FatherName'],
            'BirthDate': gregorian_birthdate,
            'BirthPlaceRef': birth_place_ref,
            'IssuancePlaceRef': issuance_place_ref,
            'Mobile': row['Mobile'],
            'Tel': row['Tel'],
            'IDSerial': row['IDSerial'],
            'Gender': gender,
            'MaritalStatus': marital_status
        })

    if not valid_records:
        print("No new records to migrate.")
        return

    # --- 5. RAHKARAN ID GENERATION & INSERTION ---
    try:
        print(f"Preparing to insert {len(valid_records)} records...")
        
        # Lock the ID table to ensure no other users grab IDs during this transaction
        dest_cursor.execute("""
            SELECT LastId 
            FROM tableIdGen WITH (UPDLOCK, HOLDLOCK) 
            WHERE TableName = 'gnr3.party'
        """)
        current_last_id = dest_cursor.fetchone()[0]
        
        insert_party_sql = """
            INSERT INTO GNR3.Party (
                PartyID, FirstName, LastName, NationalID, FatherName, BirthDate, 
                BirthPlaceRef, IssuancePlaceRef, Mobile, Tel, IDSerial, Gender, MaritalStatus,
                CreationDate, Creator, LastModificationDate, LastModificator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        
        insert_mapping_sql = """
            INSERT INTO master.dbo.PartyMigrationMapping (SourceID, DestPartyID)
            VALUES (?, ?)
        """

        # Execute inserts one by one to capture exact IDs (or you can use executemany with pre-calculated IDs)
        for record in valid_records:
            current_last_id += 1
            new_party_id = current_last_id
            
            # 1. Insert into Party Table
            dest_cursor.execute(insert_party_sql, (
                new_party_id, record['FirstName'], record['LastName'], record['NationalID'], 
                record['FatherName'], record['BirthDate'], record['BirthPlaceRef'], 
                record['IssuancePlaceRef'], record['Mobile'], record['Tel'], 
                record['IDSerial'], record['Gender'], record['MaritalStatus']
            ))
            
            # 2. Insert into Mapping Table
            dest_cursor.execute(insert_mapping_sql, (record['SourceID'], new_party_id))

        # 3. Update the LastId in the generator table
        dest_cursor.execute("""
            UPDATE tableIdGen 
            SET LastId = ? 
            WHERE TableName = 'gnr3.party'
        """, (current_last_id,))

        dest_cnxn.commit()
        print(f"Successfully migrated {len(valid_records)} records. New LastId is {current_last_id}.")

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed. Transaction rolled back. Error: {e}")

if __name__ == "__main__":
    migrate_personnel()