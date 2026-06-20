import json
import pyodbc

def get_connections():
    """Reads config.json and returns Source and Destination connections."""
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
            
        source_cnxn = pyodbc.connect(config['source_conn'])
        dest_cnxn = pyodbc.connect(config['dest_conn'], autocommit=False) 
        
        return source_cnxn, dest_cnxn
    except Exception as e:
        print(f"CRITICAL ERROR: Could not connect to databases. Details: {e}")
        raise