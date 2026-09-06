import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== PayBase for PostExtraInfo (9501/9505 area) ===")
print(pd.read_sql("""
SELECT HRS_PayBaseID, HRS_PayBaseParentID_fk, HRS_PayBaseName, HRS_PayBaseActive
FROM dbo.HRS_PayBase
WHERE HRS_PayBaseID IN (9501,9505,9500,95)
   OR HRS_PayBaseParentID_fk IN (95,9500,9501,9505)
   OR HRS_PayBaseName LIKE N'%توانیر%' OR HRS_PayBaseName LIKE N'%HRIS%'
   OR HRS_PayBaseName LIKE N'%رشته%' OR HRS_PayBaseName LIKE N'%رتبه%'
ORDER BY HRS_PayBaseID
""", src).to_string(index=False))

print("\n=== PostExtraInfo PayBase distribution ===")
print(pd.read_sql("""
SELECT pei.HRS_PayBaseID_fk, pb.HRS_PayBaseName, COUNT(*) cnt
FROM dbo.TBL_PostExtraInfo pei
LEFT JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID = pei.HRS_PayBaseID_fk
GROUP BY pei.HRS_PayBaseID_fk, pb.HRS_PayBaseName
ORDER BY cnt DESC
""", src).to_string(index=False))

print("\n=== Dept SysCode/Code samples ===")
print(pd.read_sql("""
SELECT TOP 15 TBL_DepartmentID, TBL_DepartmentName, TBL_DepartmentCode, TBL_DepartmentSysCode
FROM dbo.TBL_Department WHERE TBL_DepartmentID>0
""", src).to_string(index=False))

print("\n=== Job hierarchy (Parent/Type) ===")
print(pd.read_sql("""
SELECT TBL_JobType, COUNT(*) cnt FROM dbo.TBL_Job WHERE TBL_JobID>0 GROUP BY TBL_JobType
""", src).to_string(index=False))
print(pd.read_sql("""
SELECT TOP 20 TBL_JobID, TBL_JobParentID_fk, TBL_JobName, TBL_JobSystemCode, TBL_JobType, TBL_JobActive
FROM dbo.TBL_Job WHERE TBL_JobID>0 ORDER BY TBL_JobID
""", src).to_string(index=False))

print("\n=== Dest Job ParentRef fill ===")
print(pd.read_sql("""
SELECT COUNT(*) total,
 SUM(CASE WHEN ParentRef IS NOT NULL THEN 1 ELSE 0 END) with_parent,
 SUM(CASE WHEN JobRankNumber IS NOT NULL THEN 1 ELSE 0 END) with_ranknum
FROM HCM3.Job
""", dst).to_string(index=False))

print("\n=== Rank lookup sample ===")
print(pd.read_sql("SELECT TOP 20 Code, Value FROM SYS3.Lookup WHERE Type=N'Rank' ORDER BY Code", dst).to_string(index=False))

print("\n=== Zone / geographic ===")
sc=src.cursor()
sc.execute("""
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%Zone%' OR TABLE_NAME LIKE '%Geo%' OR TABLE_NAME LIKE '%Region%'
ORDER BY 1
""")
print([r[0] for r in sc.fetchall()])
