import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== Statute confirm/status fill ===")
print(pd.read_sql("""
SELECT
 COUNT(*) total,
 SUM(CASE WHEN ConfirmDate IS NOT NULL THEN 1 ELSE 0 END) with_confirm,
 SUM(CASE WHEN ConfirmerRef IS NOT NULL THEN 1 ELSE 0 END) with_confirmer,
 SUM(CASE WHEN PostRef IS NOT NULL THEN 1 ELSE 0 END) with_post,
 SUM(CASE WHEN Status IS NULL THEN 1 ELSE 0 END) status_null,
 Status, COUNT(*) cnt
FROM HCM3.EmployeeStatute
GROUP BY Status
""", dst).to_string(index=False))

print("\n=== TempPost table ===")
print(pd.read_sql("""
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA='HCM3' AND TABLE_NAME='EmployeeStatuteTempPost'
ORDER BY ORDINAL_POSITION
""", dst).to_string(index=False))
print("temp rows", pd.read_sql("SELECT COUNT(*) c FROM HCM3.EmployeeStatuteTempPost", dst).iloc[0,0])

print("\n=== Sample query: latest statutes per post ===")
print(pd.read_sql("""
SELECT TOP 10 s.PostRef, COUNT(*) statutes,
  SUM(CASE WHEN s.ConfirmDate IS NOT NULL THEN 1 ELSE 0 END) confirmed,
  SUM(CASE WHEN s.Status=1 THEN 1 ELSE 0 END) status1
FROM HCM3.EmployeeStatute s
WHERE s.PostRef IS NOT NULL
GROUP BY s.PostRef
ORDER BY statutes DESC
""", dst).to_string(index=False))

print("\n=== Statute Status lookup ===")
print(pd.read_sql("""
SELECT Type, Code, Value FROM SYS3.Lookup
WHERE Type LIKE N'%Statute%Status%' OR Type IN (N'StatuteStatus', N'EmployeeStatuteStatus', N'ConfirmStatus')
ORDER BY Type, Code
""", dst).to_string(index=False))
# broader
print(pd.read_sql("""
SELECT DISTINCT Type FROM SYS3.Lookup WHERE Type LIKE N'%Statute%' OR Value LIKE N'%تایید%' OR Value LIKE N'%تأييد%'
ORDER BY 1
""", dst).head(40).to_string(index=False))

print("\n=== How many posts have at least one statute ===")
print(pd.read_sql("""
SELECT
 (SELECT COUNT(DISTINCT PostRef) FROM HCM3.EmployeeStatute WHERE PostRef IS NOT NULL) posts_with_statute,
 (SELECT COUNT(*) FROM HCM3.Post) posts_total
""", dst).to_string(index=False))

print("\n=== Job FK on posts fill source ===")
print(pd.read_sql("""
SELECT
 COUNT(*) total,
 SUM(CASE WHEN TBL_JobID_fk>0 THEN 1 ELSE 0 END) with_job
FROM dbo.TBL_Post WHERE TBL_PostID>0
""", src).to_string(index=False))

print("\n=== Extra 9513 sample values (حوزه جغرافیایی) ===")
print(pd.read_sql("""
SELECT TOP 20 pei.TBL_PeiValue, COUNT(*) cnt
FROM dbo.TBL_PostExtraInfo pei
WHERE pei.HRS_PayBaseID_fk=9513
GROUP BY pei.TBL_PeiValue
ORDER BY cnt DESC
""", src).to_string(index=False))

print("\n=== Party update gap: linked without full field refresh ===")
# check if IDNumber is only updated field on match
print("mapped parties", pd.read_sql("SELECT COUNT(*) c FROM master.dbo.PartyMigrationMapping", dst).iloc[0,0])
