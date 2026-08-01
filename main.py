import json
import os
import sys
from steps import (
    step_00_cleanup,
    step_01_party,
    step_02_employee,
    step_03_education,
    step_04_military,
    step_05_relatives,
    step_06_training,
    step_07_work_record,
    step_08_warrior_record,
    step_09_org_masters,
    step_10_org_structure,
    step_11_employee_statute,
    step_12_work_record_from_statute,
    step_13_employee_research,
    step_14_employee_reward_punish,
    step_15_employee_appraisal,
    step_16_party_address,
    step_17_statute_factor,
    step_18_employee_photo,
    step_19_service_leakage_work_record,
    step_20_employment_number,
)

def get_config_path():
    """Ensures the .exe looks for config.json in the folder it is currently in."""
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(application_path, 'config.json')

def main():
    print("========================================")
    print("   DATA MIGRATION PIPELINE STARTED      ")
    print("========================================")
    
    config_path = get_config_path()
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"\nCRITICAL ERROR: 'config.json' not found at {config_path}")
        print("Please ensure config.json is in the exact same folder as this application.")
        input("\nPress Enter to exit...")
        return

    steps_to_run = config.get("steps_to_run", {})

    try:
        if steps_to_run.get("0_cleanup", False):
            step_00_cleanup.run()
        else:
            print("\n--- Skipping Step 0: Cleanup Migrated Data ---")

        if steps_to_run.get("1_party", False):
            step_01_party.run()
        else:
            print("\n--- Skipping Step 1: Party Migration ---")
            
        if steps_to_run.get("2_employee", False):
            step_02_employee.run()
        else:
            print("\n--- Skipping Step 2: Employee Migration ---")
            
        if steps_to_run.get("3_education", False):
            step_03_education.run()
        else:
            print("\n--- Skipping Step 3: Education Migration ---")

        if steps_to_run.get("4_military", False):
            step_04_military.run()
        else:
            print("\n--- Skipping Step 4: Military Migration ---")

        if steps_to_run.get("5_relatives", False):
            step_05_relatives.run()
        else:
            print("\n--- Skipping Step 5: Relatives Migration ---")

        if steps_to_run.get("6_training", False):
            step_06_training.run()
        else:
            print("\n--- Skipping Step 6: Training Migration ---")

        if steps_to_run.get("7_work_record", False):
            step_07_work_record.run()
        else:
            print("\n--- Skipping Step 7: Work Record Migration ---")

        if steps_to_run.get("8_warrior_record", False):
            step_08_warrior_record.run()
        else:
            print("\n--- Skipping Step 8: Warrior Record Migration ---")

        if steps_to_run.get("9_org_masters", False):
            step_09_org_masters.run()
        else:
            print("\n--- Skipping Step 9: Organization Masters Migration ---")

        if steps_to_run.get("10_org_structure", False):
            step_10_org_structure.run()
        else:
            print("\n--- Skipping Step 10: Organizational Structure Migration ---")

        if steps_to_run.get("11_employee_statute", False):
            step_11_employee_statute.run()
        else:
            print("\n--- Skipping Step 11: Employee Statute Migration ---")

        if steps_to_run.get("12_work_record_from_statute", False):
            step_12_work_record_from_statute.run()
        else:
            print("\n--- Skipping Step 12: Work Record from Statute Propagation ---")

        if steps_to_run.get("13_employee_research", False):
            step_13_employee_research.run()
        else:
            print("\n--- Skipping Step 13: Other Extras → EmployeeResearch ---")

        if steps_to_run.get("14_employee_reward_punish", False):
            step_14_employee_reward_punish.run()
        else:
            print("\n--- Skipping Step 14: Abet → EmployeeRewardPunish ---")

        if steps_to_run.get("15_employee_appraisal", False):
            step_15_employee_appraisal.run()
        else:
            print("\n--- Skipping Step 15: Evaluation → EmployeeAppraisal ---")

        if steps_to_run.get("16_party_address", False):
            step_16_party_address.run()
        else:
            print("\n--- Skipping Step 16: Party Address Migration ---")

        if steps_to_run.get("17_statute_factor", False):
            step_17_statute_factor.run()
        else:
            print("\n--- Skipping Step 17: Statute Factor Migration ---")

        if steps_to_run.get("18_employee_photo", False):
            step_18_employee_photo.run()
        else:
            print("\n--- Skipping Step 18: Personnel Photo Migration ---")

        if steps_to_run.get("19_service_leakage_work_record", False):
            step_19_service_leakage_work_record.run()
        else:
            print("\n--- Skipping Step 19: Service Leakage → Work Record ---")

        if steps_to_run.get("20_employment_number", False):
            step_20_employment_number.run()
        else:
            print("\n--- Skipping Step 20: Employment Number (شناسه مستخدم) ---")

        print("\n========================================")
        print("   PIPELINE COMPLETED SUCCESSFULLY      ")
        print("========================================")
        
    except Exception as e:
        print("\n========================================")
        print("   PIPELINE FAILED                      ")
        print(f"   Error Details: {e}")
        print("========================================")

if __name__ == "__main__":
    main()
    # Keep the console window open after execution
    input("\nPress Enter to exit...")
