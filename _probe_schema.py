import pyodbc, json
from pathlib import Path
cfg=json.loads(Path(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json").read_text(encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"])
dst=pyodbc.connect(cfg["dest_conn"])
sc=src.cursor(); dc=dst.cursor()

def cols(cur, schema, table):
    cur.execute("""
    SELECT c.COLUMN_NAME, c.DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_SCHEMA=? AND c.TABLE_NAME=?
    ORDER BY c.ORDINAL_POSITION
    """, schema, table)
    return [(r[0], r[1]) for r in cur.fetchall()]

def cols_any(cur, table):
    cur.execute("""
    SELECT c.TABLE_SCHEMA, c.COLUMN_NAME, c.DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_NAME=?
    ORDER BY c.TABLE_SCHEMA, c.ORDINAL_POSITION
    """, table)
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]

print("=== SRC TBL_Job ===")
print(cols_any(sc,"TBL_Job"))
print("=== SRC TBL_Post ===")
print(cols_any(sc,"TBL_Post"))
print("=== SRC TBL_Department ===")
print(cols_any(sc,"TBL_Department"))
print("=== DST Job ===")
print(cols(dc,"HCM3","Job"))
print("=== DST Post ===")
print(cols(dc,"HCM3","Post"))
print("=== DST Department ===")
print(cols(dc,"HCM3","Department"))
print("=== DST PostJob-like ===")
dc.execute("""
SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%PostJob%' OR TABLE_NAME LIKE '%JobPost%'
   OR TABLE_NAME LIKE '%Post%Extra%' OR TABLE_NAME = 'PostJob'
ORDER BY 1,2
""")
print(dc.fetchall())
dc.execute("""
SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME IN ('PostJob','Job','Post','Department','EmployeeStatute','Lookup')
ORDER BY 1,2
""")
print(dc.fetchall())
