import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

def run():
    print("\n--- Running Step 2: Employee Role Migration ---")
    
    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        print("Fetching Source IDs, Codes, and Death Dates...")
        source_df = pd.read_sql("""
            SELECT 
                TBL_PersonnelID AS SourceID, 
                TBL_PersonnelID AS Code,
                TBL_PersonnelDeathDate AS DeathDate
            FROM dbo.TBL_Personnel
        """, source_cnxn)
        
        mapping_df = pd.read_sql("SELECT SourceID, DestPartyID FROM master.dbo.PartyMigrationMapping", dest_cnxn)
        party_df = pd.read_sql("SELECT PartyID, FirstName, LastName, FullName FROM GNR3.Party", dest_cnxn)
        existing_emp_df = pd.read_sql("SELECT PartyRef FROM HCM3.Employee", dest_cnxn)

        print("Transforming and Joining Data in Memory...")
        merged_df = pd.merge(source_df, mapping_df, on='SourceID', how='inner')
        
        existing_refs = set(existing_emp_df['PartyRef'].dropna())
        merged_df = merged_df[~merged_df['DestPartyID'].isin(existing_refs)]
        
        final_df = pd.merge(merged_df, party_df, left_on='DestPartyID', right_on='PartyID', how='inner')

        if final_df.empty:
            print("No new Employee records to migrate.")
            return

        print(f"Preparing to insert {len(final_df)} Employee records...")

        dest_cursor.execute("""
            SELECT LastId 
            FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) 
            WHERE TableName = 'hcm3.employee'
        """)
        id_row = dest_cursor.fetchone()
        
        if not id_row:
            dest_cursor.execute("INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('hcm3.employee', 1000)")
            current_last_id = 1000
        else:
            current_last_id = id_row[0]

        insert_employee_sql = """
            INSERT INTO HCM3.Employee (
                EmployeeID, PartyRef, Code, Status, 
                FirstName, LastName, DeathDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """

        for index, row in final_df.iterrows():
            current_last_id += 1
            new_emp_id = current_last_id
            
            code_val = str(clean_value(row['Code']))
            raw_death_date = clean_value(row['DeathDate'])
            
            gregorian_death_date = None
            if raw_death_date:
                gregorian_death_date = shamsi_to_gregorian(raw_death_date)
            
            # --- FIXED: Fallback logic for missing mandatory names ---
            fname = row['FirstName']
            if pd.isna(fname) or str(fname).strip() in ('', 'None', 'nan'):
                fname = '-'  # Replace blank with a dash
                
            lname = row['LastName']
            if pd.isna(lname) or str(lname).strip() in ('', 'None', 'nan'):
                lname = '-'  # Replace blank with a dash
            # ---------------------------------------------------------

            dest_cursor.execute(insert_employee_sql, (
                new_emp_id, 
                row['DestPartyID'], 
                code_val, 
                1, 
                fname, 
                lname, 
                gregorian_death_date
            ))

        dest_cursor.execute("""
            UPDATE SYS3.tableIdGen 
            SET LastId = ? 
            WHERE TableName = 'HCM3.Employee'
        """, (current_last_id,))

        dest_cnxn.commit()
        print(f"Success! Migrated {len(final_df)} Employee records. New Employee LastId is {current_last_id}.")

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Employee insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()