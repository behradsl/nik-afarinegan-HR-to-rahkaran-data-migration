# main.py
from steps import step_01_base_info, step_02_personnel, step_03_contracts

def run_migration():
    print("--- Starting Migration Process ---")
    
    try:
        # Step 1: Lookups and Base Data
        print("\nExecuting Step 1: Base Information...")
        # step_01_base_info.run() 
        
        # Step 2: Core Personnel Data
        print("\nExecuting Step 2: Personnel...")
        step_02_personnel.migrate_personnel()
        
        # Step 3: Dependent Data
        print("\nExecuting Step 3: Contracts...")
        # step_03_contracts.run()
        
        print("\n--- Migration Completed Successfully ---")
        
    except Exception as e:
        print(f"\nCRITICAL ERROR: Migration halted. Details: {e}")

if __name__ == "__main__":
    run_migration()