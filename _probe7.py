import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== EmployeeStatute all post-related cols + sample ===")
cols=pd.read_sql("""
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA='HCM3' AND TABLE_NAME='EmployeeStatute'
ORDER BY ORDINAL_POSITION
""", dst)
print(cols.to_string(index=False))
print(pd.read_sql("""
SELECT TOP 3 EmployeeStatuteID, EmployeeRef, PostRef, OrganizationalStructureRef, Status, ExpiryDate, ApplyDate, RankCode
FROM HCM3.EmployeeStatute ORDER BY EmployeeStatuteID DESC
""", dst).to_string(index=False))

print("\n=== Looking for appointment/history tables ===")
dc=dst.cursor()
dc.execute("""
SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%Appoint%' OR TABLE_NAME LIKE '%Post%Hist%'
   OR TABLE_NAME LIKE '%Occupation%' OR TABLE_NAME LIKE '%Statute%'
ORDER BY 1,2
""")
print(dc.fetchall())

print("\n=== Src PostAllotmentHistory cols ===")
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='TBL_PostAllotmentHistory' ORDER BY ORDINAL_POSITION
""", src).to_string(index=False))

print("\n=== Party match: UPDATE? sample linked count ===")
print(pd.read_sql("""
SELECT COUNT(*) mapped FROM master.dbo.PartyMigrationMapping
""", dst).to_string(index=False))

print("\n=== Employment date on employee/personnel ===")
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='TBL_Personnel' AND (
  COLUMN_NAME LIKE '%Employ%' OR COLUMN_NAME LIKE '%Start%' OR COLUMN_NAME LIKE '%Date%'
)
ORDER BY 1
""", src).to_string(index=False))
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA='HCM3' AND TABLE_NAME='Employee'
  AND (COLUMN_NAME LIKE '%Employ%' OR COLUMN_NAME LIKE '%Start%' OR COLUMN_NAME LIKE '%Date%')
ORDER BY 1
""", dst).to_string(index=False))

print("\n=== Dest Post counts / mapped ===")
print("src", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0", src).iloc[0,0])
print("mapped", pd.read_sql("SELECT COUNT(*) c FROM master.dbo.PostMigrationMapping", dst).iloc[0,0])
print("dest", pd.read_sql("SELECT COUNT(*) c FROM HCM3.Post", dst).iloc[0,0])
print("orphan dest posts (not in mapping)", pd.read_sql("""
SELECT COUNT(*) c FROM HCM3.Post p
WHERE NOT EXISTS (SELECT 1 FROM master.dbo.PostMigrationMapping m WHERE m.DestPostID=p.PostID)
""", dst).iloc[0,0])

print("\n=== Org structure counts ===")
print(pd.read_sql("""
SELECT NodeKind, COUNT(*) cnt FROM master.dbo.OrgStructureMigrationMapping GROUP BY NodeKind
""", dst).to_string(index=False))
print("src depts", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Department WHERE TBL_DepartmentID>0", src).iloc[0,0])
print("src posts with parent", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0 AND ISNULL(TBL_PostParentID_fk,0)>0", src).iloc[0,0])
print("src posts with Oc", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0 AND ISNULL(TBL_OcID_fk,0)>0", src).iloc[0,0])
print("Oc count", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_OrganizationChart WHERE TBL_OcID>0", src).iloc[0,0])
