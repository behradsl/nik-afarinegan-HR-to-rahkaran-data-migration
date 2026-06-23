import json
import pyodbc
import os
import sys

def get_config_path():
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(application_path, 'config.json')

def get_connections():
    """Reads config.json and returns Source and Destination connections."""
    try:
        with open(get_config_path(), 'r') as f:
            config = json.load(f)
            
        source_cnxn = pyodbc.connect(config['source_conn'])
        dest_cnxn = pyodbc.connect(config['dest_conn'], autocommit=False) 
        
        return source_cnxn, dest_cnxn
    except Exception as e:
        print(f"CRITICAL ERROR: Could not connect to databases. Details: {e}")
        raise