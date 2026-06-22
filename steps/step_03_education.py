import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, normalize_persian
from utils.date_helpers import shamsi_to_gregorian

warnings.filterwarnings('ignore', category=UserWarning)

def run():
    print("\n--- Running Step 3: Education Data Migration ---")
    
    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        print("Fetching Source Education History...")
        source_query = """
            SELECT 
                dh.TBL_PersonnelId_fk AS SourceID,
                d.TBL_DegreeName AS DegreeName,
                db.TBL_DbName AS DisciplineName,
                dh.HRS_DhAverage AS GPA,
                dh.HRS_DhExcuteDate AS EndDate
            FROM dbo.HRS_DegreeHistory dh
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = dh.TBL_DegreeId_fk
            LEFT JOIN dbo.TBL_DegreeBranch db ON db.TBL_DbID = dh.TBL_DbID_fk
            WHERE dh.TBL_PersonnelId_fk IS NOT NULL
        """
        source_df = pd.read_sql(source_query, source_cnxn)
        
        print("Mapping Source to Rahkaran Employee IDs...")
        mapping_query = """
            SELECT m.SourceID, e.EmployeeID 
            FROM master.dbo.PartyMigrationMapping m
            JOIN HCM3.Employee e ON m.DestPartyID = e.PartyRef
        """
        emp_map_df = pd.read_sql(mapping_query, dest_cnxn)
        
        merged_df = pd.merge(source_df, emp_map_df, on='SourceID', how='inner')
        
        if merged_df.empty:
            print("No matching employees found. Skipping Education step.")
            return
            
        print("Cleaning and Normalizing Text...")
        merged_df['DegreeName'] = merged_df['DegreeName'].apply(lambda x: normalize_persian(clean_value(x)))
        merged_df = merged_df.dropna(subset=['DegreeName']) # Must have at least a Degree
        
        # FIXED: Fallback for missing Disciplines (Rahkaran requires this column)
        merged_df['DisciplineName'] = merged_df['DisciplineName'].apply(
            lambda x: 'نامشخص' if pd.isna(clean_value(x)) else normalize_persian(clean_value(x))
        )
        
        print("Synchronizing Education Lookups (Degrees and Disciplines)...")
        
        def sync_lookup(lookup_type, unique_values):
            lookup_df = pd.read_sql(f"SELECT Code, Value FROM SYS3.Lookup WHERE Type = '{lookup_type}'", dest_cnxn)
            existing_map = {normalize_persian(row['Value']): int(row['Code']) for _, row in lookup_df.iterrows()}
            
            missing_values = [v for v in unique_values if v and v not in existing_map]
            
            if missing_values:
                print(f"  -> Found {len(missing_values)} missing {lookup_type}s. Adding to SYS3.Lookup...")
                
                dest_cursor.execute("SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) WHERE TableName = 'sys3.lookup'")
                id_row = dest_cursor.fetchone()
                
                current_last_id = int(id_row[0]) if id_row else 10000
                max_code = int(lookup_df['Code'].max()) if not lookup_df.empty else 0
                
                insert_lookup_sql = """
                    INSERT INTO SYS3.Lookup (
                        LookupID, Type, Code, Value, DisplayOrder, System, CanEdit, CanDelete
                    ) VALUES (?, ?, ?, ?, ?, 'HCM3', 1, 1)
                """
                
                for val in missing_values:
                    current_last_id += 1
                    max_code += 1
                    dest_cursor.execute(insert_lookup_sql, (
                        current_last_id, lookup_type, max_code, val, max_code - 1
                    ))
                    existing_map[val] = max_code 
                
                if id_row:
                    dest_cursor.execute("UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'sys3.lookup'", (current_last_id,))
                else:
                    dest_cursor.execute("INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('sys3.lookup', ?)", (current_last_id,))
                
            return existing_map

        degree_map = sync_lookup('EducationDegree', merged_df['DegreeName'].unique())
        discipline_map = sync_lookup('EducationDiscipline', merged_df['DisciplineName'].unique())
        
        print("Preparing to insert Employee Education records...")
        existing_edu_df = pd.read_sql("SELECT EmployeeRef, DegreeCode, DisciplineCode FROM HCM3.EmployeeEducation", dest_cnxn)
        existing_edu_set = set(zip(existing_edu_df['EmployeeRef'], existing_edu_df['DegreeCode'], existing_edu_df['DisciplineCode']))
        
        valid_records = []
        for _, row in merged_df.iterrows():
            emp_id = int(row['EmployeeID'])
            deg_code = int(degree_map[row['DegreeName']])
            
            # This is now guaranteed to have a value (at least "نامشخص")
            disc_code = int(discipline_map[row['DisciplineName']]) 
            
            if (emp_id, deg_code, disc_code) in existing_edu_set:
                continue
                
            raw_end_date = clean_value(row['EndDate'])
            gregorian_end_date = shamsi_to_gregorian(raw_end_date) if raw_end_date else None
            
            gpa = None
            raw_gpa = clean_value(row['GPA'])
            if raw_gpa is not None:
                try:
                    gpa = float(raw_gpa)
                except ValueError:
                    pass
            
            valid_records.append((emp_id, deg_code, disc_code, gregorian_end_date, gpa))
            
        if not valid_records:
            print("No new Employee Education records to migrate.")
            dest_cnxn.commit() 
            return
            
        dest_cursor.execute("SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) WHERE TableName = 'hcm3.employeeeducation'")
        id_row = dest_cursor.fetchone()
        current_last_id = int(id_row[0]) if id_row else 1000
        
        insert_edu_sql = """
            INSERT INTO HCM3.EmployeeEducation (
                EmployeeEducationID, EmployeeRef, DegreeCode, DisciplineCode, 
                EndDate, GPA, CenterCode, NeedLevelCode, QualityCode, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, ISNULL(?, GETDATE()), GETDATE(), 1, GETDATE(), 1)
        """
        
        for record in valid_records:
            current_last_id += 1
            emp_id, deg_code, disc_code, end_date, gpa = record
            
            dest_cursor.execute(insert_edu_sql, (
                current_last_id, emp_id, deg_code, disc_code, 
                end_date, gpa, end_date
            ))
            
        if id_row:
            dest_cursor.execute("UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'hcm3.employeeeducation'", (current_last_id,))
        else:
            dest_cursor.execute("INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('hcm3.employeeeducation', ?)", (current_last_id,))
            
        dest_cnxn.commit()
        print(f"Success! Migrated {len(valid_records)} Education records. New LastId is {current_last_id}.")
        
    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Education insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()