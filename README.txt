Rahkaran HR migration — source tables and lookups
==================================================

Data is copied from the source personnel database into Rahkaran.
People and organization rows go into HCM3 and GNR3.
Code lists go into SYS3.Lookup.
Each migrated row is also recorded in a master.dbo mapping table so a
later run updates the same destination row instead of inserting a second one.


Source and destination tables
-----------------------------

Source                              Destination                                                         What it is
----------------------------------  ------------------------------------------------------------------  ------------------------------------------
TBL_Personnel                       GNR3.Party                                                          The person
TBL_Personnel                       HCM3.Employee                                                       The employee
TBL_Personnel                       GNR3.Address, GNR3.PartyAddress                                     Address
TBL_PersonnelImage                  HCM3.EmployeeSupplementary                                          Photo
TBL_PersonnelPeroperty              HCM3.EmployeeEmploymentNumber                                       شناسه مستخدم
HRS_DegreeHistory                   HCM3.EmployeeEducation                                              Education
HRS_MilitaryHistory                 HCM3.Employee (military columns)                                    Military service
HRS_SponsorShip                     HCM3.EmployeeRelative, EmployeeMarriage, EmployeeRelativeInsurance  Relatives
LMS_EducationHistory                HCM3.EmployeeTraining                                               Training
HRS_EmploymentServiceHistory        HCM3.EmployeeWorkRecord                                             Work history
HRS_ServiceLeakage                  HCM3.EmployeeWorkRecord                                             Extra work-history rows
HRS_SacrificeHistory                HCM3.EmployeeWarriorRecord                                          Warrior record
HRS_OtherExtraHistory               HCM3.EmployeeResearch                                               Research
HRS_AbetHistory                     HCM3.EmployeeRewardPunish                                           Reward and punishment
HRS_EvaluationPersonnel             HCM3.EmployeeAppraisal                                              Appraisal
TBL_Department                      HCM3.Department                                                     Departments
TBL_Post                            HCM3.Post                                                           Posts
TBL_Job                             HCM3.Job                                                            Jobs
TBL_Post                            HCM3.PostJob                                                        Post-job link
TBL_EmploymentType                  HCM3.EmploymentType                                                 Employment types
TBL_Place                           SYS3.Lookup (work location)                                         Statute work location
TBL_Zone                            GNR3.RegionalDivision                                               Geography on the post
TBL_OrganizationChart, TBL_Post     HCM3.OrganizationalStructure and its item and description tables    Structure versions, 1350 through 1402
HRS_RuleType                        HCM3.StatuteType                                                    Statute types
HRS_RuleDocument                    HCM3.EmployeeStatute                                                The حکم
PAY_PayrollFactor                   HCM3.StatuteFactor, Formula, StatuteFactorProperty                  Factor definitions
HRS_RuleDocumentDetail              HCM3.EmployeeStatuteFactor                                          Factor prices on each حکم
HRS_RuleDocumentScores              HCM3.EmployeeStatuteFactor                                          Score and service-history amounts on each حکم

Code lists such as degree, rank, post type, relation, and military branch
are read from HRS_PayBase, TBL_JobGrade, and LMS_LmsBase and written to
SYS3.Lookup.

HCM3.EmployeeWorkRecord is also updated from the migrated حکم. That step
does not read another source table. It copies post, department, and the
other statute fields onto work-history rows that already have a post and
a department.


Lookups
-------

Lookups are the code lists in SYS3.Lookup. A row stores a type, a numeric
code, and the title shown on the screen.

Filled from the source title. If that title is already in the lookup, the
existing code is reused. If it is new, the next code is added.

Lookup                    Source                                              What it is
------------------------  --------------------------------------------------  --------------------------------
EducationDegree           TBL_Degree                                          Degree name on education and work history
EducationDiscipline       TBL_DegreeBranch                                    Field of study
EducationCenter           HRS_UnivercityCenter                                University or school
CourseSubject             LMS_LmsBase (certificate type)                      Training subject
ResearchType              PAY_PayrollFactor name on HRS_OtherExtraHistory     Research type
AppraisalType             HRS_PayBase name of the assessment type             Appraisal type
ResignationReason         HRS_PayBase title on HRS_ServiceLeakage             Reason on a service-interruption work record
EmployeeRelativeExtra1    HRS_PayBase parent 77                               علت ایجاد تحت تکفل
WorkLocation              TBL_Place                                           Work location on the حکم
PostLevel                 TBL_JobGrade                                        رتبه شغلی on the post
PostType                  HRS_PayBase parent 64                               نوع پست
PostExtra1                TBL_Post.TBL_PostOrganazationPostCode               کد توانیر
PostExtra2                TBL_PostExtraInfo where the PayBase is 9505         شناسه HRS

Filled with a fixed code. The source id is translated to the code below.

Lookup                              Source                         Codes
----------------------------------  -----------------------------  --------------------------------------------------------------------
EducationNeedLevel                  HRS_DegreeScore.HRS_DsNeedDegree  1 یک, 2 دو, 3 سه, 4 چهار. درجه نیاز آموزشی
EmployeeEducationExtra1             HRS_DhStatusId_fk              3601 -> 1 مرتبط, 3602 -> 2 مشابه, 3603 -> 3 غیر مرتبط. وضعیت مدرک
MilitaryBranch                      HRS_MhStatusId_fk              3701 -> 1 مرتبط, 3702 -> 3 مشابه, 3703 -> 2 غیر مرتبط
ExemptionType                       (no source value is written)   Existing labels restored: 1 دائم, 2 موقت
WorkType                            HRS_EshType                    1 داخل شرکت, 2 دولتی داخل صنعت, 3 دولتی خارج صنعت, 4 خصوصی خارج صنعت, 5 خصوصی داخل صنعت
WorkRelationType                    HRS_JobRelationID_fk           300001 -> 1 مرتبط, 300002 -> 2 غیر مرتبط, 300003 -> 3 مشابه, 300004 -> 4 محاسبه فقط در سنوات, 300005 -> 5 هنوز مشخص نشده است
WorkRecordExtra1                    HRS_EshActive                  1 فعال, 2 غیرفعال
WorkRecordExtra2                    HRS_EshHeadingStatus           0 بدون حق سرپرستی, 1 دارای حق سرپرستی, 2 دارای حق سرپرستی (نوع ۲)
EmployeeTrainingExtra1              LMS_EhTakeScore                0 بدون تعلق امتیاز, 1 تعلق امتیاز
EmployeeTrainingExtra2              Training status flags          0 عملکرد آموزشی, 1 دوره های گذرانده, 2 گذرانده نیازمند اثربخشی, 3 دارای امتیاز, 5 گذرانده بدون گواهینامه
EducationState                      Relative status                150011 -> 1 محصل, 150005 -> 2 دانشجو
PhysicalState                       Relative status                150016 -> 2 معلول. Other relatives stay 1 سالم
MaritalStatus, PersonMaritalStatus  HRS_MaritalStatusID_fk         20001 -> 1 مجرد, 20002 -> 2 متأهل, 20003 -> 3 معیل, 20004 -> 4 زن سرپرست خانوار. Code 4 was added
EmployeeStatuteExtra1               HRS_RdPersonalGrade            Codes 1 through 20. The screen title is رتبه. The displayed value is the same number
Rank                                HRS_RdJobGrade, PayBase parent 68  680001 -> 1 مقدماتی, 680002 -> 2 پایه, 680003 -> 3 ارشد, 680004 -> 4 خبره, 680005 -> 5 عالی. The screen title is طبقه شغلی
RegionalDivisionType                HRS_PayBase under parent 70    The lookup code is the source PayBase id. This is حوزه جغرافیایی on the post (شهرستانی, استانی, ملی, and the level)

These codes already existed in Rahkaran, and the migration only chooses one.

Lookup                              Mapping
----------------------------------  --------------------------------------------------------------------
Gender                              10001 -> 1 مرد, 10002 -> 2 زن
Religion                            Every person is 1 اسلام
Subreligion                         Every person is 1 شیعه
EmployeeRelation                    30005 -> 1 پدر, 30006 -> 2 مادر, 30003 -> 7 پسر, 30004 -> 8 دختر. A spouse is stored as a marriage row
WarriorGroup                        PayBase parent 8: family of a martyr -> 1, جانباز -> 2, رزمنده -> 3, آزاده -> 4, بسیج -> 6
StatuteFactorType                   Factors under parent 2 are 1 اصلی. Parent 22 and سابقه خدمت are 2 فرعی
EmployeeStatuteStatus               Every migrated حکم is 10 تأیید پرداخت
IssueTime, AfterIssueStatus         Copied from the statute type. New types are created as 1
