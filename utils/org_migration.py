"""Shared org master migration helpers (Department, Post, Job, EmploymentType, Place)."""
import pandas as pd
from utils.data_helpers import clean_value, clean_persian_text, normalize_persian
from utils.lookup_helpers import sync_lookup, ensure_lookup_codes

DEFAULT_TITLE = '-'
DEFAULT_JOB_CLASS_CODE = 1


def ensure_table_id(cursor, table_name, default_last_id=0):
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


def setup_department_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'DepartmentMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.DepartmentMigrationMapping (
                SourceDepartmentID BIGINT PRIMARY KEY,
                DestDepartmentID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_post_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'PostMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.PostMigrationMapping (
                SourcePostID BIGINT PRIMARY KEY,
                DestPostID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_job_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'JobMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.JobMigrationMapping (
                SourceJobID BIGINT PRIMARY KEY,
                DestJobID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_employment_type_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'EmploymentTypeMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.EmploymentTypeMigrationMapping (
                SourceEmploymentTypeID INT PRIMARY KEY,
                DestEmploymentTypeID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_place_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'PlaceMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.PlaceMigrationMapping (
                SourcePlaceID INT PRIMARY KEY,
                DestWorkLocationCode INT NOT NULL,
                PlaceName NVARCHAR(200) NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_place_regional_division_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'PlaceRegionalDivisionMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.PlaceRegionalDivisionMapping (
                SourcePlaceID INT PRIMARY KEY,
                DestRegionalDivisionID BIGINT NOT NULL,
                PlaceName NVARCHAR(200) NULL,
                MatchKind NVARCHAR(20) NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


RD_TYPE_COUNTRY = 1
RD_TYPE_PROVINCE = 2
RD_TYPE_CITY = 3


def _get_iran_regional_division_id(dest_cursor):
    dest_cursor.execute("""
        SELECT TOP 1 RegionalDivisionID
        FROM GNR3.RegionalDivision
        WHERE Type = ?
        ORDER BY RegionalDivisionID
    """, (RD_TYPE_COUNTRY,))
    row = dest_cursor.fetchone()
    if not row:
        raise RuntimeError("Country RegionalDivision (Type=1) not found in destination.")
    return int(row[0])


def _insert_city_under_iran(dest_cursor, name, iran_id):
    """Insert Type=city node as rightmost child of Iran (nested-set Left/Right)."""
    dest_cursor.execute("""
        SELECT [Right]
        FROM GNR3.RegionalDivision WITH (UPDLOCK, HOLDLOCK)
        WHERE RegionalDivisionID = ?
    """, (iran_id,))
    row = dest_cursor.fetchone()
    if not row:
        raise RuntimeError(f"Iran RegionalDivisionID={iran_id} not found.")
    parent_right = int(row[0])

    dest_cursor.execute(
        "UPDATE GNR3.RegionalDivision SET [Right] = [Right] + 2 WHERE [Right] >= ?",
        (parent_right,),
    )
    dest_cursor.execute(
        "UPDATE GNR3.RegionalDivision SET [Left] = [Left] + 2 WHERE [Left] >= ?",
        (parent_right,),
    )

    new_id = ensure_table_id(dest_cursor, 'GNR3.RegionalDivision', 0) + 1
    dest_cursor.execute("""
        INSERT INTO GNR3.RegionalDivision (
            RegionalDivisionID, Name, ParentRef, Type, [Left], [Right]
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (new_id, name, iran_id, RD_TYPE_CITY, parent_right, parent_right + 1))
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'GNR3.RegionalDivision'",
        (new_id,),
    )
    return new_id


def ensure_places_as_regional_divisions(source_cnxn, dest_cnxn, dest_cursor):
    """
    Resolve TBL_Place -> GNR3.RegionalDivisionID for Post.RegionalDivisionRef.

    Rules (by cleaned place name vs RegionalDivision.Name):
      - province + city match -> city
      - province only -> province
      - city only -> city
      - no match -> create Type=city under Iran country
    """
    setup_place_regional_division_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_PlaceID AS SourcePlaceID,
            TBL_PlaceName AS PlaceName
        FROM dbo.TBL_Place
        WHERE TBL_PlaceID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourcePlaceID, DestRegionalDivisionID "
        "FROM master.dbo.PlaceRegionalDivisionMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourcePlaceID']): int(row['DestRegionalDivisionID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source places for RegionalDivision.")
        return result

    source_df['PlaceNameClean'] = source_df['PlaceName'].apply(clean_persian_text)
    missing_df = source_df[~source_df['SourcePlaceID'].isin(result.keys())].copy()
    missing_df = missing_df[missing_df['PlaceNameClean'].notna()]

    if missing_df.empty:
        print(f"  -> Places already mapped to RegionalDivision: {len(result)}.")
        return result

    rd_df = pd.read_sql("""
        SELECT RegionalDivisionID, Name, Type
        FROM GNR3.RegionalDivision
        WHERE Type IN (?, ?)
    """, dest_cnxn, params=[RD_TYPE_PROVINCE, RD_TYPE_CITY])
    rd_df['NameClean'] = rd_df['Name'].apply(clean_persian_text)

    # name -> list of (id, type)
    by_name = {}
    for _, row in rd_df.iterrows():
        name = row['NameClean']
        if not name:
            continue
        by_name.setdefault(name, []).append(
            (int(row['RegionalDivisionID']), int(row['Type']))
        )

    iran_id = _get_iran_regional_division_id(dest_cursor)
    insert_mapping_sql = """
        INSERT INTO master.dbo.PlaceRegionalDivisionMapping (
            SourcePlaceID, DestRegionalDivisionID, PlaceName, MatchKind
        ) VALUES (?, ?, ?, ?)
    """

    created = 0
    matched = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourcePlaceID'])
        name = row['PlaceNameClean']

        candidates = list(by_name.get(name, []))
        province_name = f'استان {name}'
        for item in by_name.get(province_name, []):
            if item not in candidates:
                candidates.append(item)

        provinces = [cid for cid, typ in candidates if typ == RD_TYPE_PROVINCE]
        cities = [cid for cid, typ in candidates if typ == RD_TYPE_CITY]

        if cities:
            dest_id = min(cities)
            match_kind = 'city'
        elif provinces:
            dest_id = min(provinces)
            match_kind = 'province'
        else:
            dest_id = _insert_city_under_iran(dest_cursor, name, iran_id)
            by_name.setdefault(name, []).append((dest_id, RD_TYPE_CITY))
            match_kind = 'created'
            created += 1

        dest_cursor.execute(
            insert_mapping_sql, (source_id, dest_id, name, match_kind)
        )
        result[source_id] = dest_id
        matched += 1

    print(
        f"  -> Places→RegionalDivision mapped: {matched} "
        f"(created cities: {created}). Total: {len(result)}."
    )
    return result


def setup_org_structure_mapping_table(cursor):
    """
    Mapping for org-structure nodes.
    NodeKind: 'D' = department node (PostRef NULL), 'P' = post node.
    SourceID: TBL_DepartmentID or TBL_PostID.
    Does not drop existing data — caller must clear before schema upgrade.
    """
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'OrgStructureMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.OrgStructureMigrationMapping (
                SourceOcID INT NOT NULL,
                NodeKind CHAR(1) NOT NULL,
                SourceID BIGINT NOT NULL,
                DestOrganizationalStructureID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE(),
                PRIMARY KEY (SourceOcID, NodeKind, SourceID)
            )
        END
    """)
    cursor.commit()


def upgrade_org_structure_mapping_schema(cursor):
    """Drop legacy post-only mapping table after data has been cleared."""
    cursor.execute("""
        IF EXISTS (SELECT * FROM master.sys.tables WHERE name = 'OrgStructureMigrationMapping')
          AND NOT EXISTS (
            SELECT 1 FROM master.sys.columns
            WHERE object_id = OBJECT_ID('master.dbo.OrgStructureMigrationMapping')
              AND name = 'NodeKind'
          )
        BEGIN
            DROP TABLE master.dbo.OrgStructureMigrationMapping
        END
    """)
    cursor.commit()
    setup_org_structure_mapping_table(cursor)


def setup_org_structure_description_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'OrgStructureDescriptionMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.OrgStructureDescriptionMigrationMapping (
                SourceOcID INT PRIMARY KEY,
                DestDescriptionID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_statute_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'StatuteMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.StatuteMigrationMapping (
                SourceRuleDocumentID BIGINT PRIMARY KEY,
                DestEmployeeStatuteID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def setup_statute_type_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'StatuteTypeMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.StatuteTypeMigrationMapping (
                SourceRuleTypeID INT PRIMARY KEY,
                DestStatuteTypeID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _title_and_code(title_raw, code_raw, source_id, title_max=200, code_max=100):
    title = clean_persian_text(title_raw)
    if not title:
        title = DEFAULT_TITLE
    title = title[:title_max]

    code = clean_value(code_raw)
    if code is not None:
        code = str(code).strip() or None
    if not code or code == '0':
        code = str(source_id)
    code = code[:code_max]
    return title, code


def _unique_code_for_title(base_code, title, source_id, used_pairs, code_max=100):
    code = base_code[:code_max]
    if (code, title) not in used_pairs:
        return code
    suffix = f"-{source_id}"
    code = f"{base_code[:max(0, code_max - len(suffix))]}{suffix}"
    if (code, title) not in used_pairs:
        return code
    return str(source_id)[:code_max]


def _active_status(active_raw):
    try:
        return 1 if int(float(active_raw)) == 1 else 2
    except (TypeError, ValueError):
        return 2


def ensure_departments(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_Department -> HCM3.Department. Returns SourceDepartmentID -> DestDepartmentID."""
    setup_department_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_DepartmentID AS SourceDepartmentID,
            TBL_DepartmentName AS DepartmentName,
            TBL_DepartmentCode AS DepartmentCode,
            TBL_DepartmentActive AS DepartmentActive
        FROM dbo.TBL_Department
        WHERE TBL_DepartmentID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourceDepartmentID, DestDepartmentID FROM master.dbo.DepartmentMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourceDepartmentID']): int(row['DestDepartmentID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source departments found.")
        return result

    missing_df = source_df[~source_df['SourceDepartmentID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Departments already mapped: {len(result)}.")
        return result

    existing_pairs_df = pd.read_sql(
        "SELECT Code, Title FROM HCM3.Department WHERE Code IS NOT NULL",
        dest_cnxn,
    )
    used_pairs = {
        (str(row['Code']), normalize_persian(str(row['Title'])) if row['Title'] else DEFAULT_TITLE)
        for _, row in existing_pairs_df.iterrows()
    }

    last_id = ensure_table_id(dest_cursor, 'HCM3.Department', 0)
    insert_sql = """
        INSERT INTO HCM3.Department (
            DepartmentID, Code, Title, RegionalDivisionRef, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, NULL, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.DepartmentMigrationMapping (
            SourceDepartmentID, DestDepartmentID
        ) VALUES (?, ?)
    """

    inserted = 0
    uniquified = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourceDepartmentID'])
        title, base_code = _title_and_code(
            row['DepartmentName'], row['DepartmentCode'], source_id, title_max=200
        )
        code = _unique_code_for_title(base_code, title, source_id, used_pairs)
        if code != base_code:
            uniquified += 1
        status = _active_status(row['DepartmentActive'])
        last_id += 1
        dest_cursor.execute(insert_sql, (last_id, code, title, status))
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        used_pairs.add((code, title))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Department'",
        (last_id,),
    )
    print(
        f"  -> Departments inserted: {inserted} "
        f"(codes uniquified: {uniquified}). Total mapped: {len(result)}."
    )
    return result


def ensure_post_levels_from_job_grades(source_cnxn, dest_cnxn, dest_cursor):
    """
    Sync TBL_JobGrade titles into SYS3.Lookup PostLevel.
    Returns SourceJobGradeID -> DestLevelCode.
    """
    source_df = pd.read_sql("""
        SELECT
            TBL_JgID AS SourceJobGradeID,
            TBL_JgDescription AS GradeName
        FROM dbo.TBL_JobGrade
        WHERE TBL_JgID > 0
    """, source_cnxn)

    if source_df.empty:
        print("  -> No source job grades for PostLevel.")
        return {}

    source_df['GradeNameClean'] = source_df['GradeName'].apply(clean_persian_text)
    source_df = source_df[source_df['GradeNameClean'].notna()].copy()
    if source_df.empty:
        print("  -> No usable job grade titles for PostLevel.")
        return {}

    name_to_code = sync_lookup(
        dest_cnxn,
        dest_cursor,
        'PostLevel',
        source_df['GradeNameClean'].unique(),
    )

    result = {}
    for _, row in source_df.iterrows():
        code = name_to_code.get(row['GradeNameClean'])
        if code is None:
            continue
        result[int(row['SourceJobGradeID'])] = int(code)

    print(f"  -> PostLevel codes ready for {len(result)} job grade(s).")
    return result


def ensure_post_types_from_paybase(source_cnxn, dest_cnxn, dest_cursor):
    """
    Sync PayBase نوع پست (parent 64) titles into SYS3.Lookup PostType.
    Returns SourcePostTypeID (PayBaseID) -> DestTypeCode.
    """
    source_df = pd.read_sql("""
        SELECT
            HRS_PayBaseID AS SourcePostTypeID,
            HRS_PayBaseName AS TypeName
        FROM dbo.HRS_PayBase
        WHERE HRS_PayBaseParentID_fk = 64
          AND HRS_PayBaseID > 0
    """, source_cnxn)

    if source_df.empty:
        print("  -> No source post types for PostType.")
        return {}

    source_df['TypeNameClean'] = source_df['TypeName'].apply(clean_persian_text)
    source_df = source_df[source_df['TypeNameClean'].notna()].copy()
    if source_df.empty:
        print("  -> No usable post type titles for PostType.")
        return {}

    name_to_code = sync_lookup(
        dest_cnxn,
        dest_cursor,
        'PostType',
        source_df['TypeNameClean'].unique(),
    )

    result = {}
    for _, row in source_df.iterrows():
        code = name_to_code.get(row['TypeNameClean'])
        if code is None:
            continue
        result[int(row['SourcePostTypeID'])] = int(code)

    print(f"  -> PostType codes ready for {len(result)} post type(s).")
    return result


def ensure_posts(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_Post -> HCM3.Post. Returns SourcePostID -> DestPostID."""
    setup_post_mapping_table(dest_cursor)

    jg_to_level = ensure_post_levels_from_job_grades(source_cnxn, dest_cnxn, dest_cursor)
    type_to_code = ensure_post_types_from_paybase(source_cnxn, dest_cnxn, dest_cursor)
    place_to_rd = ensure_places_as_regional_divisions(source_cnxn, dest_cnxn, dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_PostID AS SourcePostID,
            TBL_PostTitle AS PostTitle,
            TBL_PostCode AS PostCode,
            TBL_PostActive AS PostActive,
            TBL_JgID_fk AS SourceJobGradeID,
            TBL_PlaceID_fk AS SourcePlaceID,
            TBL_PostTypeID_fk AS SourcePostTypeID
        FROM dbo.TBL_Post
        WHERE TBL_PostID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourcePostID, DestPostID FROM master.dbo.PostMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourcePostID']): int(row['DestPostID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source posts found.")
        return result

    def _lookup_code(row, column, mapping):
        raw = row[column]
        if pd.isna(raw):
            return None
        try:
            key = int(raw)
        except (TypeError, ValueError):
            return None
        if key <= 0:
            return None
        return mapping.get(key)

    def _level_code(row):
        return _lookup_code(row, 'SourceJobGradeID', jg_to_level)

    def _type_code(row):
        return _lookup_code(row, 'SourcePostTypeID', type_to_code)

    def _regional_ref(row):
        return _lookup_code(row, 'SourcePlaceID', place_to_rd)

    missing_df = source_df[~source_df['SourcePostID'].isin(result.keys())]
    inserted = 0
    if not missing_df.empty:
        last_id = ensure_table_id(dest_cursor, 'HCM3.Post', 0)
        insert_sql = """
            INSERT INTO HCM3.Post (
                PostID, Code, Title, LevelCode, TypeCode, RegionalDivisionRef, Status,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.PostMigrationMapping (
                SourcePostID, DestPostID
            ) VALUES (?, ?)
        """

        for _, row in missing_df.iterrows():
            source_id = int(row['SourcePostID'])
            title, code = _title_and_code(
                row['PostTitle'], row['PostCode'], source_id, title_max=400
            )
            status = _active_status(row['PostActive'])
            level_code = _level_code(row)
            type_code = _type_code(row)
            regional_ref = _regional_ref(row)
            last_id += 1
            dest_cursor.execute(
                insert_sql,
                (last_id, code, title, level_code, type_code, regional_ref, status),
            )
            dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
            result[source_id] = last_id
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Post'",
            (last_id,),
        )
        print(f"  -> Posts inserted: {inserted}. Total mapped: {len(result)}.")
    else:
        print(f"  -> Posts already mapped: {len(result)}.")

    # Backfill LevelCode / TypeCode / RegionalDivisionRef on already-mapped posts.
    updated_level = 0
    updated_type = 0
    updated_rd = 0
    update_level_sql = """
        UPDATE HCM3.Post
        SET LevelCode = ?, LastModificationDate = GETDATE(), LastModifier = 1
        WHERE PostID = ?
          AND ISNULL(LevelCode, -2147483648) <> ISNULL(?, -2147483648)
    """
    update_type_sql = """
        UPDATE HCM3.Post
        SET TypeCode = ?, LastModificationDate = GETDATE(), LastModifier = 1
        WHERE PostID = ?
          AND ISNULL(TypeCode, -2147483648) <> ISNULL(?, -2147483648)
    """
    update_rd_sql = """
        UPDATE HCM3.Post
        SET RegionalDivisionRef = ?, LastModificationDate = GETDATE(), LastModifier = 1
        WHERE PostID = ?
          AND ISNULL(RegionalDivisionRef, -1) <> ISNULL(?, -1)
    """
    for _, row in source_df.iterrows():
        source_id = int(row['SourcePostID'])
        dest_id = result.get(source_id)
        if dest_id is None:
            continue
        level_code = _level_code(row)
        dest_cursor.execute(update_level_sql, (level_code, dest_id, level_code))
        if dest_cursor.rowcount:
            updated_level += 1
        type_code = _type_code(row)
        dest_cursor.execute(update_type_sql, (type_code, dest_id, type_code))
        if dest_cursor.rowcount:
            updated_type += 1
        regional_ref = _regional_ref(row)
        dest_cursor.execute(update_rd_sql, (regional_ref, dest_id, regional_ref))
        if dest_cursor.rowcount:
            updated_rd += 1

    if updated_level:
        print(f"  -> Posts LevelCode updated: {updated_level}.")
    if updated_type:
        print(f"  -> Posts TypeCode updated: {updated_type}.")
    if updated_rd:
        print(f"  -> Posts RegionalDivisionRef updated: {updated_rd}.")

    return result


def ensure_jobs(source_cnxn, dest_cnxn, dest_cursor, source_job_ids=None):
    """
    Migrate TBL_Job -> HCM3.Job.
    If source_job_ids is provided, only those IDs (plus already mapped) are considered for insert.
    Returns SourceJobID -> DestJobID.
    """
    setup_job_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_JobID AS SourceJobID,
            TBL_JobName AS JobName,
            TBL_JobSystemCode AS JobCode,
            TBL_JobActive AS JobActive
        FROM dbo.TBL_Job
        WHERE TBL_JobID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourceJobID, DestJobID FROM master.dbo.JobMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourceJobID']): int(row['DestJobID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source jobs found.")
        return result

    if source_job_ids is not None:
        wanted = {int(j) for j in source_job_ids if j is not None and int(j) > 0}
        source_df = source_df[source_df['SourceJobID'].isin(wanted)]

    missing_df = source_df[~source_df['SourceJobID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Jobs already mapped: {len(result)}.")
        return result

    existing_pairs_df = pd.read_sql(
        "SELECT Code, Title FROM HCM3.Job WHERE Code IS NOT NULL",
        dest_cnxn,
    )
    used_pairs = {
        (str(row['Code']), normalize_persian(str(row['Title'])) if row['Title'] else DEFAULT_TITLE)
        for _, row in existing_pairs_df.iterrows()
    }

    last_id = ensure_table_id(dest_cursor, 'HCM3.Job', 0)
    insert_sql = """
        INSERT INTO HCM3.Job (
            JobID, Code, Title, ClassCode, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.JobMigrationMapping (
            SourceJobID, DestJobID
        ) VALUES (?, ?)
    """

    inserted = 0
    uniquified = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourceJobID'])
        title, base_code = _title_and_code(
            row['JobName'], row['JobCode'], source_id, title_max=400
        )
        code = _unique_code_for_title(base_code, title, source_id, used_pairs)
        if code != base_code:
            uniquified += 1
        status = _active_status(row['JobActive'])
        last_id += 1
        dest_cursor.execute(
            insert_sql,
            (last_id, code, title, DEFAULT_JOB_CLASS_CODE, status),
        )
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        used_pairs.add((code, title))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Job'",
        (last_id,),
    )
    print(
        f"  -> Jobs inserted: {inserted} "
        f"(codes uniquified: {uniquified}). Total mapped: {len(result)}."
    )
    return result


def ensure_employment_types(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_EmploymentType -> HCM3.EmploymentType."""
    setup_employment_type_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_EtID AS SourceEmploymentTypeID,
            TBL_EtName AS EtName,
            TBL_EtActive AS EtActive
        FROM dbo.TBL_EmploymentType
        WHERE TBL_EtID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourceEmploymentTypeID, DestEmploymentTypeID "
        "FROM master.dbo.EmploymentTypeMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourceEmploymentTypeID']): int(row['DestEmploymentTypeID'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source employment types found.")
        return result

    missing_df = source_df[~source_df['SourceEmploymentTypeID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Employment types already mapped: {len(result)}.")
        return result

    last_id = ensure_table_id(dest_cursor, 'HCM3.EmploymentType', 0)
    insert_sql = """
        INSERT INTO HCM3.EmploymentType (
            EmploymentTypeID, RawTitle, Title,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.EmploymentTypeMigrationMapping (
            SourceEmploymentTypeID, DestEmploymentTypeID
        ) VALUES (?, ?)
    """

    inserted = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourceEmploymentTypeID'])
        title = clean_persian_text(row['EtName']) or DEFAULT_TITLE
        title = title[:400]
        last_id += 1
        dest_cursor.execute(insert_sql, (last_id, title, title))
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.EmploymentType'",
        (last_id,),
    )
    print(f"  -> Employment types inserted: {inserted}. Total mapped: {len(result)}.")
    return result


def ensure_places_as_work_locations(source_cnxn, dest_cnxn, dest_cursor):
    """
    Migrate TBL_Place titles into SYS3.Lookup WorkLocation.
    Returns SourcePlaceID -> DestWorkLocationCode.
    """
    setup_place_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_PlaceID AS SourcePlaceID,
            TBL_PlaceName AS PlaceName
        FROM dbo.TBL_Place
        WHERE TBL_PlaceID > 0
    """, source_cnxn)

    existing_df = pd.read_sql(
        "SELECT SourcePlaceID, DestWorkLocationCode, PlaceName "
        "FROM master.dbo.PlaceMigrationMapping",
        dest_cnxn,
    )
    result = {
        int(row['SourcePlaceID']): int(row['DestWorkLocationCode'])
        for _, row in existing_df.iterrows()
    }

    if source_df.empty:
        print("  -> No source places found.")
        return result

    source_df['PlaceNameClean'] = source_df['PlaceName'].apply(clean_persian_text)
    missing_df = source_df[~source_df['SourcePlaceID'].isin(result.keys())].copy()
    missing_df = missing_df[missing_df['PlaceNameClean'].notna()]

    if missing_df.empty:
        print(f"  -> Places already mapped: {len(result)}.")
        return result

    name_to_code = sync_lookup(
        dest_cnxn,
        dest_cursor,
        'WorkLocation',
        missing_df['PlaceNameClean'].unique(),
    )

    insert_mapping_sql = """
        INSERT INTO master.dbo.PlaceMigrationMapping (
            SourcePlaceID, DestWorkLocationCode, PlaceName
        ) VALUES (?, ?, ?)
    """
    inserted = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourcePlaceID'])
        name = row['PlaceNameClean']
        code = name_to_code.get(name)
        if code is None:
            continue
        dest_cursor.execute(insert_mapping_sql, (source_id, int(code), name))
        result[source_id] = int(code)
        inserted += 1

    print(f"  -> Places mapped: {inserted}. Total mapped: {len(result)}.")
    return result


def ensure_rank_codes_from_grades(dest_cnxn, dest_cursor, grade_values):
    """
    Ensure Rank lookup codes for personal grades (code = grade, value = رتبه {n}).
    Returns set of ensured codes.
    """
    code_to_value = {}
    for g in grade_values:
        try:
            code = int(g)
        except (TypeError, ValueError):
            continue
        if code <= 0:
            continue
        code_to_value[code] = f'رتبه {code}'
    if not code_to_value:
        return {}
    return ensure_lookup_codes(dest_cnxn, dest_cursor, 'Rank', code_to_value)
