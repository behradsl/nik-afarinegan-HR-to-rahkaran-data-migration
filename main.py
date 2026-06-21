from steps import step_01_party, step_02_employee

# --- CONFIGURATION: TOGGLE STEPS HERE ---
STEPS_TO_RUN = {
    "1_party": True,      # Set to False to skip Party migration
    "2_employee": True    # Set to True to run Employee migration
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
            
        print("\n========================================")
        print("   PIPELINE COMPLETED SUCCESSFULLY      ")
        print("========================================")
        
    except Exception as e:
        print("\n========================================")
        print("   PIPELINE FAILED                      ")
        print("========================================")

if __name__ == "__main__":
    main()