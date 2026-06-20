import jdatetime

def shamsi_to_gregorian(shamsi_str):
    """Converts a Shamsi date string (YYYY/MM/DD) to standard Gregorian (YYYY-MM-DD)."""
    if not shamsi_str or str(shamsi_str).strip() == '':
        return None
    try:
        parts = str(shamsi_str).split('/')
        if len(parts) == 3:
            y = int(parts[0])
            m = int(parts[1])
            d = int(parts[2])
            
            # Fix legacy system dates where month or day is '00'
            if m == 0: m = 1
            if d == 0: d = 1
            
            jdate = jdatetime.date(y, m, d)
            return jdate.togregorian().strftime('%Y-%m-%d')
    except Exception as e:
        # If it still fails (e.g. 1388/02/32), silently return NULL instead of spamming the console
        pass
        
    return None