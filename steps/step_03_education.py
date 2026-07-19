import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_value, normalize_persian
from utils.date_helpers import shamsi_to_gregorian
from utils.lookup_helpers import ensure_degree_mappings, sync_lookup

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
                dh.TBL_DegreeId_fk AS SourceDegreeID,
                d.TBL_DegreeName AS DegreeName,
                db.TBL_DbName AS DisciplineName,
                uc.HRS_UcName AS CenterName,
                dh.HRS_DhAverage AS GPA,
                dh.HRS_DhEnterDate AS StartDate,
                dh.HRS_DhRecieveDate AS EndDate,
                dh.HRS_DhExcuteDate AS EffectiveDate
            FROM dbo.HRS_DegreeHistory dh
            LEFT JOIN dbo.TBL_Degree d ON d.TBL_DegreeID = dh.TBL_DegreeId_fk
            LEFT JOIN dbo.TBL_DegreeBranch db ON db.TBL_DbID = dh.TBL_DbID_fk
            LEFT JOIN dbo.HRS_UnivercityCenter uc ON uc.HRS_UCId = dh.HRS_UCId_fk 
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
        merged_df = merged_df.dropna(subset=['DegreeName']) 
        
        merged_df['DisciplineName'] = merged_df['DisciplineName'].apply(
            lambda x: 'نامشخص' if pd.isna(clean_value(x)) else normalize_persian(clean_value(x))
        )
        
        merged_df['CenterName'] = merged_df['CenterName'].apply(
            lambda x: 'نامشخص' if pd.isna(clean_value(x)) else normalize_persian(clean_value(x))
        )
        
        print("Synchronizing Education Lookups (Degrees, Disciplines, and Centers)...")
        degree_id_map = ensure_degree_mappings(
            source_cnxn,
            dest_cnxn,
            dest_cursor,
            merged_df[['SourceDegreeID', 'DegreeName']],
        )
        discipline_map = sync_lookup(
            dest_cnxn, dest_cursor, 'EducationDiscipline', merged_df['DisciplineName'].unique()
        )
        center_map = sync_lookup(
            dest_cnxn, dest_cursor, 'EducationCenter', merged_df['CenterName'].unique()
        )
        
        print("Preparing to insert Employee Education records...")
        existing_edu_df = pd.read_sql(
            "SELECT EmployeeRef, DegreeCode, DisciplineCode FROM HCM3.EmployeeEducation",
            dest_cnxn,
        )
        existing_edu_set = set(zip(
            existing_edu_df['EmployeeRef'],
            existing_edu_df['DegreeCode'],
            existing_edu_df['DisciplineCode'],
        ))
        
        valid_records = []
        for _, row in merged_df.iterrows():
            source_degree_id = row['SourceDegreeID']
            try:
                source_degree_id = int(source_degree_id)
            except (TypeError, ValueError):
                continue
            if source_degree_id <= 0 or source_degree_id not in degree_id_map:
                continue

            emp_id = int(row['EmployeeID'])
            deg_code = int(degree_id_map[source_degree_id])
            disc_code = int(discipline_map[row['DisciplineName']]) 
            center_code = int(center_map[row['CenterName']])
            
            if (emp_id, deg_code, disc_code) in existing_edu_set:
                continue
            
            start_date = shamsi_to_gregorian(clean_value(row['StartDate']))
            end_date = shamsi_to_gregorian(clean_value(row['EndDate']))
            effective_date = shamsi_to_gregorian(clean_value(row['EffectiveDate']))
            
            gpa = None
            raw_gpa = clean_value(row['GPA'])
            if raw_gpa is not None:
                try:
                    gpa = float(raw_gpa)
                except ValueError:
                    pass
            
            valid_records.append((emp_id, deg_code, disc_code, center_code, start_date, end_date, effective_date, gpa))
            
        if not valid_records:
            print("No new Employee Education records to migrate.")
            dest_cnxn.commit() 
            return
            
        dest_cursor.execute(
            "SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) WHERE TableName = 'hcm3.employeeeducation'"
        )
        id_row = dest_cursor.fetchone()
        current_last_id = int(id_row[0]) if id_row else 1000
        
        insert_edu_sql = """
            INSERT INTO HCM3.EmployeeEducation (
                EmployeeEducationID, EmployeeRef, DegreeCode, DisciplineCode, CenterCode,
                StartDate, EndDate, GPA, NeedLevelCode, QualityCode, EffectiveDate,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ISNULL(?, GETDATE()), GETDATE(), 1, GETDATE(), 1)
        """
        
        for record in valid_records:
            current_last_id += 1
            emp_id, deg_code, disc_code, center_code, start_date, end_date, effective_date, gpa = record
            
            dest_cursor.execute(insert_edu_sql, (
                current_last_id, emp_id, deg_code, disc_code, center_code, 
                start_date, end_date, gpa, effective_date
            ))
            
        if id_row:
            dest_cursor.execute(
                "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'hcm3.employeeeducation'",
                (current_last_id,),
            )
        else:
            dest_cursor.execute(
                "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('hcm3.employeeeducation', ?)",
                (current_last_id,),
            )
            
        dest_cnxn.commit()
        print(f"Success! Migrated {len(valid_records)} Education records. New LastId is {current_last_id}.")
        
    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Education insertion. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
