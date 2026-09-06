import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== Post duplicates by Code+Title ===")
print(pd.read_sql("""
SELECT Code, Title, COUNT(*) cnt
FROM HCM3.Post
GROUP BY Code, Title
HAVING COUNT(*)>1
ORDER BY cnt DESC
""", dst).head(20).to_string(index=False))
print("dup groups:", len(pd.read_sql("""
SELECT Code, Title FROM HCM3.Post GROUP BY Code, Title HAVING COUNT(*)>1
""", dst)))

print("\n=== Mapping vs Post counts ===")
print(pd.read_sql("""
SELECT
 (SELECT COUNT(*) FROM dbo.TBL_Post WHERE TBL_PostID>0) src,
 (SELECT COUNT(*) FROM master.dbo.PostMigrationMapping) mapped,
 (SELECT COUNT(*) FROM HCM3.Post) dest
""", dst).to_string(index=False))
# need cross-db - do separately
print("src posts", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0", src).iloc[0,0])
print("mapped", pd.read_sql("SELECT COUNT(*) c FROM master.dbo.PostMigrationMapping", dst).iloc[0,0])
print("dest", pd.read_sql("SELECT COUNT(*) c FROM HCM3.Post", dst).iloc[0,0])

print("\n=== OrgStructure vs source tree ===")
print("src depts", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Department WHERE TBL_DepartmentID>0", src).iloc[0,0])
print("src posts with Oc", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0 AND TBL_OcID_fk>0", src).iloc[0,0])
print("src posts total", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0", src).iloc[0,0])
print("src posts with parent", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_Post WHERE TBL_PostID>0 AND TBL_PostParentID_fk>0", src).iloc[0,0])
print(pd.read_sql("""
SELECT NodeKind, COUNT(*) cnt FROM master.dbo.OrgStructureMigrationMapping GROUP BY NodeKind
""", dst).to_string(index=False))
print(pd.read_sql("""
SELECT COUNT(*) os,
 SUM(CASE WHEN PostRef IS NULL THEN 1 ELSE 0 END) dept_nodes,
 SUM(CASE WHEN PostRef IS NOT NULL THEN 1 ELSE 0 END) post_nodes
FROM HCM3.OrganizationalStructure
""", dst).to_string(index=False))

print("\n=== Statute PostRef / TempPost / Status fill ===")
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA='HCM3' AND TABLE_NAME='EmployeeStatute'
  AND (COLUMN_NAME LIKE '%Post%' OR COLUMN_NAME LIKE '%Status%' OR COLUMN_NAME LIKE '%Confirm%' OR COLUMN_NAME LIKE '%Temp%')
ORDER BY 1
""", dst).to_string(index=False))
print(pd.read_sql("""
SELECT
 COUNT(*) total,
 SUM(CASE WHEN PostRef IS NOT NULL THEN 1 ELSE 0 END) with_post,
 SUM(CASE WHEN OrganizationalStructureRef IS NOT NULL THEN 1 ELSE 0 END) with_os,
 SUM(CASE WHEN ExpiryDate IS NOT NULL THEN 1 ELSE 0 END) with_exp,
 SUM(CASE WHEN Status=1 THEN 1 ELSE 0 END) status1
FROM HCM3.EmployeeStatute
""", dst).to_string(index=False))
