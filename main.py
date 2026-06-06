from steps import step_01_party

def main():
    print("========================================")
    print("   DATA MIGRATION PIPELINE STARTED      ")
    print("========================================")
    
    try:
        # Execute Step 1
        step_01_party.run()
        
        # Space for future steps
        # print("\n--- Running Step 2: Employee Data ---")
        # step_02_employee.run()
        
        print("\n========================================")
        print("   PIPELINE COMPLETED SUCCESSFULLY      ")
        print("========================================")
        
    except Exception as e:
        print("\n========================================")
        print("   PIPELINE FAILED                      ")
        print(f"   Error: {e}")
        print("========================================")

if __name__ == "__main__":
    main()