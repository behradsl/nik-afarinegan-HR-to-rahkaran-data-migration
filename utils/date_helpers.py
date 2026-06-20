import jdatetime

def shamsi_to_gregorian(shamsi_str):
    """Converts a Shamsi date string (YYYY/MM/DD) to standard Gregorian (YYYY-MM-DD)."""
    if not shamsi_str or str(shamsi_str).strip() == '':
        return None
    try:
        parts = str(shamsi_str).split('/')
        if len(parts) == 3:
            jdate = jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
            return jdate.togregorian().strftime('%Y-%m-%d')
    except Exception as e:
        print(f"Warning: Date conversion error for '{shamsi_str}': {e}")
    return None