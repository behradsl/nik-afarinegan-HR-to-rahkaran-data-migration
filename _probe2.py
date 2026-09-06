import pyodbc, json
from pathlib import Path
cfg=json.loads(Path(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json").read_text(encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])
sc=src.cursor(); dc=dst.cursor()

print("=== DST PostJob cols ===")
dc.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='HCM3' AND TABLE_NAME='PostJob' ORDER BY ORDINAL_POSITION")
print(dc.fetchall())

print("=== SRC tables with Job/Rank/Grade ===")
sc.execute("""
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE='BASE TABLE' AND (
  TABLE_NAME LIKE '%Job%' OR TABLE_NAME LIKE '%Rank%' OR TABLE_NAME LIKE '%Grade%'
  OR TABLE_NAME LIKE '%رتبه%' OR TABLE_NAME LIKE '%Post%')
ORDER BY TABLE_NAME
""")
print([r[0] for r in sc.fetchall()])

print("=== SRC HRS_RuleDocument sample cols related archive ===")
sc.execute("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='HRS_RuleDocument' ORDER BY ORDINAL_POSITION
""")
print([r[0] for r in sc.fetchall()])

print("=== Lookups PostExtra ===")
dc.execute("""
SELECT TOP 50 Type, Code, Value FROM SYS3.Lookup
WHERE Type LIKE '%Post%Extra%' OR Type IN ('PostExtra1','PostExtra2','RegionalDivisionType')
ORDER BY Type, Code
""")
print(dc.fetchall())

print("=== Distinct Lookup Types with Extra/Regional/JobRank ===")
dc.execute("""
SELECT DISTINCT Type FROM SYS3.Lookup
WHERE Type LIKE '%Extra%' OR Type LIKE '%Regional%' OR Type LIKE '%Rank%' OR Type LIKE '%Job%'
ORDER BY 1
""")
print([r[0] for r in dc.fetchall()])
