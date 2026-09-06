import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
dst=pyodbc.connect(cfg["dest_conn"]); src=pyodbc.connect(cfg["source_conn"])
print(pd.read_sql("""
SELECT COUNT(*) orphan_below_map FROM HCM3.Post WHERE PostID < 13925
""", dst).to_string(index=False))
print(pd.read_sql("""
SELECT COUNT(*) mapped FROM master.dbo.PostMigrationMapping
""", dst).to_string(index=False))
print("JobGrade count", pd.read_sql("SELECT COUNT(*) c FROM dbo.TBL_JobGrade WHERE TBL_JgID>0", src).iloc[0,0])
print(pd.read_sql("""
SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%JobRank%' OR TABLE_NAME='Rank'
ORDER BY 1,2
""", dst).to_string(index=False))
print("Dept code vs sys fill", pd.read_sql("""
SELECT
 SUM(CASE WHEN TBL_DepartmentCode IS NOT NULL AND LTRIM(RTRIM(TBL_DepartmentCode)) NOT IN ('','0') THEN 1 ELSE 0 END) code_filled,
 SUM(CASE WHEN TBL_DepartmentSysCode IS NOT NULL AND LTRIM(RTRIM(TBL_DepartmentSysCode)) NOT IN ('','0') THEN 1 ELSE 0 END) sys_filled,
 COUNT(*) total
FROM dbo.TBL_Department WHERE TBL_DepartmentID>0
""", src).to_string(index=False))
print("Posts Level/Type fill dest", pd.read_sql("""
SELECT
 SUM(CASE WHEN LevelCode IS NOT NULL THEN 1 ELSE 0 END) level_filled,
 SUM(CASE WHEN TypeCode IS NOT NULL THEN 1 ELSE 0 END) type_filled,
 SUM(CASE WHEN RegionalDivisionRef IS NOT NULL THEN 1 ELSE 0 END) rd_filled
FROM HCM3.Post p
WHERE EXISTS (SELECT 1 FROM master.dbo.PostMigrationMapping m WHERE m.DestPostID=p.PostID)
""", dst).to_string(index=False))
