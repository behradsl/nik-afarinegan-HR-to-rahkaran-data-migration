from steps import step_01_party, step_02_employee, step_03_education

# --- CONFIGURATION: TOGGLE STEPS HERE ---
STEPS_TO_RUN = {
    "1_party": False,
    "2_employee": False,
    "3_education": True   # <--- Set this to True to run only education
}
# -----------------------------------------

def main():
    print("========================================")
    print("   DATA MIGRATION PIPELINE STARTED      ")
    print("========================================")
    
    try:
        if STEPS_TO_RUN.get("1_party"):
            step_01_party.run()
        else:
            print("\n--- Skipping Step 1: Party Migration ---")
            
        if STEPS_TO_RUN.get("2_employee"):
            step_02_employee.run()
        else:
            print("\n--- Skipping Step 2: Employee Migration ---")
            
        if STEPS_TO_RUN.get("3_education"):
            step_03_education.run()
        else:
            print("\n--- Skipping Step 3: Education Migration ---")
            
        print("\n========================================")
        print("   PIPELINE COMPLETED SUCCESSFULLY      ")
        print("========================================")
        
    except Exception as e:
        print("\n========================================")
        print("   PIPELINE FAILED                      ")
        print("========================================")

if __name__ == "__main__":
    main()