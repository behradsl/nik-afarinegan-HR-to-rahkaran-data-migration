# utils/data_helpers.py
import pandas as pd

def clean_value(val):
    """Converts NaNs, '0', 0, and empty strings to None (SQL NULL)."""
    if pd.isna(val): 
        return None
    val_str = str(val).strip()
    if val_str in ('', '0', '0.0', 'None'): 
        return None
    return val

def normalize_persian(text):
    """Standardizes Arabic/Persian characters and removes extra spacing."""
    if not isinstance(text, str):
        return text
    # Fix Ye and Ke
    text = text.replace('ي', 'ی').replace('ك', 'ک')
    # Collapse multiple spaces into a single space and trim ends
    text = ' '.join(text.split())
    return text