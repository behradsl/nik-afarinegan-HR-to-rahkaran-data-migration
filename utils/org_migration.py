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


def setup_org_structure_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM master.sys.tables WHERE name = 'OrgStructureMigrationMapping')
        BEGIN
            CREATE TABLE master.dbo.OrgStructureMigrationMapping (
                SourcePostID BIGINT NOT NULL,
                SourceOcID INT NOT NULL,
                DestOrganizationalStructureID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE(),
                PRIMARY KEY (SourcePostID, SourceOcID)
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


def ensure_posts(source_cnxn, dest_cnxn, dest_cursor):
    """Migrate TBL_Post -> HCM3.Post. Returns SourcePostID -> DestPostID."""
    setup_post_mapping_table(dest_cursor)

    source_df = pd.read_sql("""
        SELECT
            TBL_PostID AS SourcePostID,
            TBL_PostTitle AS PostTitle,
            TBL_PostCode AS PostCode,
            TBL_PostActive AS PostActive
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

    missing_df = source_df[~source_df['SourcePostID'].isin(result.keys())]
    if missing_df.empty:
        print(f"  -> Posts already mapped: {len(result)}.")
        return result

    last_id = ensure_table_id(dest_cursor, 'HCM3.Post', 0)
    insert_sql = """
        INSERT INTO HCM3.Post (
            PostID, Code, Title, TypeCode, RegionalDivisionRef, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, NULL, NULL, ?, GETDATE(), 1, GETDATE(), 1)
    """
    insert_mapping_sql = """
        INSERT INTO master.dbo.PostMigrationMapping (
            SourcePostID, DestPostID
        ) VALUES (?, ?)
    """

    inserted = 0
    for _, row in missing_df.iterrows():
        source_id = int(row['SourcePostID'])
        title, code = _title_and_code(
            row['PostTitle'], row['PostCode'], source_id, title_max=400
        )
        status = _active_status(row['PostActive'])
        last_id += 1
        dest_cursor.execute(insert_sql, (last_id, code, title, status))
        dest_cursor.execute(insert_mapping_sql, (source_id, last_id))
        result[source_id] = last_id
        inserted += 1

    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Post'",
        (last_id,),
    )
    print(f"  -> Posts inserted: {inserted}. Total mapped: {len(result)}.")
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
