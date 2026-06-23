import json
import os
import sys
from steps import step_01_party, step_02_employee, step_03_education

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