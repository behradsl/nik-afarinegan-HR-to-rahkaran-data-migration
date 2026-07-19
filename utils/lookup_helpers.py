import pandas as pd
from utils.data_helpers import clean_value, normalize_persian


def setup_degree_mapping_table(cursor):
    """Creates DegreeMigrationMapping in master if it does not exist."""
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'DegreeMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.DegreeMigrationMapping (
                SourceDegreeID INT PRIMARY KEY,
                DestDegreeCode INT NOT NULL,
                DegreeName NVARCHAR(200) NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def sync_lookup(dest_cnxn, dest_cursor, lookup_type, unique_values):
    """
    Ensures each value exists in SYS3.Lookup for the given Type.
    Returns dict: normalized name -> Code (int).
    """
    lookup_df = pd.read_sql(
        f"SELECT Code, Value FROM SYS3.Lookup WHERE Type = '{lookup_type}'",
        dest_cnxn,
    )
    existing_map = {
        normalize_persian(row['Value']): int(row['Code'])
        for _, row in lookup_df.iterrows()
    }

    missing_values = [v for v in unique_values if v and v not in existing_map]

    if missing_values:
        print(f"  -> Found {len(missing_values)} missing {lookup_type}s. Adding to SYS3.Lookup...")

        dest_cursor.execute(
            "SELECT LastId FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK) WHERE TableName = 'sys3.lookup'"
        )
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
            dest_cursor.execute(
                "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'sys3.lookup'",
                (current_last_id,),
            )
        else:
            dest_cursor.execute(
                "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES ('sys3.lookup', ?)",
                (current_last_id,),
            )

    return existing_map


def ensure_degree_mappings(source_cnxn, dest_cnxn, dest_cursor, degree_rows):
    """
    Sync EducationDegree lookups and persist SourceDegreeID -> DestDegreeCode.

    degree_rows: iterable of dict-like rows with SourceDegreeID and DegreeName
                 (or a DataFrame with those columns).
    Returns dict: SourceDegreeID (int) -> DestDegreeCode (int).
    """
    setup_degree_mapping_table(dest_cursor)

    if isinstance(degree_rows, pd.DataFrame):
        pairs_df = degree_rows[['SourceDegreeID', 'DegreeName']].copy()
    else:
        pairs_df = pd.DataFrame(list(degree_rows), columns=['SourceDegreeID', 'DegreeName'])

    if pairs_df.empty:
        return {}

    pairs_df['SourceDegreeID'] = pd.to_numeric(pairs_df['SourceDegreeID'], errors='coerce')
    pairs_df = pairs_df.dropna(subset=['SourceDegreeID'])
    pairs_df['SourceDegreeID'] = pairs_df['SourceDegreeID'].astype(int)
    pairs_df = pairs_df[pairs_df['SourceDegreeID'] > 0]

    pairs_df['DegreeName'] = pairs_df['DegreeName'].apply(
        lambda x: normalize_persian(clean_value(x))
    )
    pairs_df = pairs_df.dropna(subset=['DegreeName'])
    pairs_df = pairs_df.drop_duplicates(subset=['SourceDegreeID'], keep='first')

    if pairs_df.empty:
        return {}

    existing_map_df = pd.read_sql(
        "SELECT SourceDegreeID, DestDegreeCode FROM master.dbo.DegreeMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourceDegreeID']): int(row['DestDegreeCode'])
        for _, row in existing_map_df.iterrows()
    }

    missing_df = pairs_df[~pairs_df['SourceDegreeID'].isin(result.keys())]
    if missing_df.empty:
        return {sid: result[sid] for sid in pairs_df['SourceDegreeID'] if sid in result}

    name_to_code = sync_lookup(
        dest_cnxn,
        dest_cursor,
        'EducationDegree',
        missing_df['DegreeName'].unique(),
    )

    insert_sql = """
        IF NOT EXISTS (
            SELECT 1 FROM master.dbo.DegreeMigrationMapping WHERE SourceDegreeID = ?
        )
        BEGIN
            INSERT INTO master.dbo.DegreeMigrationMapping (SourceDegreeID, DestDegreeCode, DegreeName)
            VALUES (?, ?, ?)
        END
        ELSE
        BEGIN
            UPDATE master.dbo.DegreeMigrationMapping
            SET DestDegreeCode = ?, DegreeName = ?
            WHERE SourceDegreeID = ?
        END
    """

    for _, row in missing_df.iterrows():
        source_id = int(row['SourceDegreeID'])
        name = row['DegreeName']
        dest_code = name_to_code.get(name)
        if dest_code is None:
            continue
        dest_code = int(dest_code)
        dest_cursor.execute(
            insert_sql,
            (source_id, source_id, dest_code, name, dest_code, name, source_id),
        )
        result[source_id] = dest_code

    return {sid: result[sid] for sid in pairs_df['SourceDegreeID'] if sid in result}
