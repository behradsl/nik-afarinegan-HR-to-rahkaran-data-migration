import pandas as pd
from utils.data_helpers import clean_value, clean_persian_text, normalize_persian
from utils.rahkaran_cache import invalidate_lookup_cache

# Matches destination app / SYS3.tableIdGen key (Version is SQL timestamp — never insert it).
LOOKUP_IDGEN_TABLE = 'Sys3.Lookup'
LOOKUP_INSERT_SQL = """
    INSERT INTO SYS3.Lookup (
        LookupID, Type, Code, Value, DisplayOrder, Extra, System, CanEdit, CanDelete
    ) VALUES (?, ?, ?, ?, ?, N'', 'HCM3', 1, 1)
"""


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


def repair_lookup_extra(dest_cursor, lookup_type=None):
    """
    App UI inserts Extra as empty string; NULL Extra can break lookup screens.
    Version is rowversion/timestamp — left untouched.
    """
    if lookup_type:
        dest_cursor.execute(
            """
            UPDATE SYS3.Lookup
            SET Extra = N''
            WHERE Extra IS NULL AND Type = ?
            """,
            (lookup_type,),
        )
    else:
        dest_cursor.execute(
            """
            UPDATE SYS3.Lookup
            SET Extra = N''
            WHERE Extra IS NULL AND System = N'HCM3'
            """
        )
    repaired = dest_cursor.rowcount if dest_cursor.rowcount is not None else 0
    if repaired:
        print(f"  -> Repaired Extra on {repaired} Lookup row(s).")
    return repaired


def _next_lookup_id(dest_cursor):
    dest_cursor.execute(
        """
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
        """,
        (LOOKUP_IDGEN_TABLE,),
    )
    id_row = dest_cursor.fetchone()
    current_last_id = int(id_row[0]) if id_row else 10000
    return current_last_id, id_row is not None


def _bump_lookup_idgen(dest_cursor, current_last_id, idgen_exists):
    if idgen_exists:
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (current_last_id, LOOKUP_IDGEN_TABLE),
        )
    else:
        dest_cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, ?)",
            (LOOKUP_IDGEN_TABLE, current_last_id),
        )


LOOKUP_INFO_IDGEN_TABLE = 'SYS3.LookupInfo'


def ensure_lookup_info(dest_cursor, lookup_type, title, *, is_dynamic=True):
    """
    Ensure a SYS3.LookupInfo row exists for lookup_type.
    Updates Title when the row already exists and title differs.
    """
    dest_cursor.execute(
        "SELECT LookupInfoID, Title FROM SYS3.LookupInfo WHERE Type = ?",
        (lookup_type,),
    )
    row = dest_cursor.fetchone()
    if row:
        existing_title = row[1]
        if title and existing_title != title:
            dest_cursor.execute(
                "UPDATE SYS3.LookupInfo SET Title = ? WHERE Type = ?",
                (title, lookup_type),
            )
            print(f"  -> LookupInfo title updated for {lookup_type}: {title}")
            invalidate_lookup_cache()
        return int(row[0])

    dest_cursor.execute(
        """
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
        """,
        (LOOKUP_INFO_IDGEN_TABLE,),
    )
    id_row = dest_cursor.fetchone()
    last_id = int(id_row[0]) if id_row else 0
    last_id += 1
    dest_cursor.execute(
        """
        INSERT INTO SYS3.LookupInfo (LookupInfoID, Type, Title, IsDynamic)
        VALUES (?, ?, ?, ?)
        """,
        (last_id, lookup_type, title, 1 if is_dynamic else 0),
    )
    if id_row:
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (last_id, LOOKUP_INFO_IDGEN_TABLE),
        )
    else:
        dest_cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, ?)",
            (LOOKUP_INFO_IDGEN_TABLE, last_id),
        )
    print(f"  -> LookupInfo created for {lookup_type}: {title}")
    invalidate_lookup_cache()
    return last_id


def ensure_lookup_codes(dest_cnxn, dest_cursor, lookup_type, code_to_value, *, overwrite_values=False):
    """
    Ensure fixed Code→Value pairs exist in SYS3.Lookup for lookup_type.
    Inserts missing codes. When overwrite_values=True, also updates Value on
    existing codes when it differs (after Persian normalize).
    Returns dict: code (int) -> value (str) for the requested codes after ensure.
    """
    repair_lookup_extra(dest_cursor, lookup_type)

    result = {}
    inserted = 0
    updated = 0

    current_last_id, idgen_exists = _next_lookup_id(dest_cursor)

    for code, value in sorted((int(c), v) for c, v in code_to_value.items()):
        value = normalize_persian(value) if value else value
        dest_cursor.execute(
            """
            SELECT LookupID, Value
            FROM SYS3.Lookup
            WHERE Type = ? AND Code = ?
            """,
            (lookup_type, code),
        )
        row = dest_cursor.fetchone()
        if row:
            existing_value = row[1]
            if (
                overwrite_values
                and value is not None
                and normalize_persian(existing_value or '') != value
            ):
                dest_cursor.execute(
                    """
                    UPDATE SYS3.Lookup
                    SET Value = ?, Extra = N''
                    WHERE Type = ? AND Code = ?
                    """,
                    (value, lookup_type, code),
                )
                updated += 1
                result[code] = value
            else:
                result[code] = existing_value
            continue

        current_last_id += 1
        dest_cursor.execute(
            LOOKUP_INSERT_SQL,
            (current_last_id, lookup_type, code, value, max(code - 1, 0)),
        )
        result[code] = value
        inserted += 1

    if inserted:
        print(f"  -> Added {inserted} missing {lookup_type} code(s).")
        _bump_lookup_idgen(dest_cursor, current_last_id, idgen_exists)
    if updated:
        print(f"  -> Updated Value on {updated} existing {lookup_type} code(s).")
    elif not result:
        # Nothing inserted and nothing found — still return requested defaults
        result = {int(c): normalize_persian(v) for c, v in code_to_value.items()}

    if inserted or updated:
        invalidate_lookup_cache()
    return result


def sync_lookup(dest_cnxn, dest_cursor, lookup_type, unique_values):
    """
    Ensures each value exists in SYS3.Lookup for the given Type.
    Returns dict: normalized name -> Code (int).
    """
    repair_lookup_extra(dest_cursor, lookup_type)

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

        current_last_id, idgen_exists = _next_lookup_id(dest_cursor)
        max_code = int(lookup_df['Code'].max()) if not lookup_df.empty else 0

        for val in missing_values:
            current_last_id += 1
            max_code += 1
            dest_cursor.execute(LOOKUP_INSERT_SQL, (
                current_last_id, lookup_type, max_code, val, max_code - 1
            ))
            existing_map[val] = max_code

        _bump_lookup_idgen(dest_cursor, current_last_id, idgen_exists)
        invalidate_lookup_cache()

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

    pairs_df['DegreeName'] = pairs_df['DegreeName'].apply(clean_persian_text)
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
