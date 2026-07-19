from datetime import datetime

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
    except Exception:
        # If it still fails (e.g. 1388/02/32), silently return NULL instead of spamming the console
        pass
        
    return None


def months_between(start_date_str, end_date_str):
    """Whole months between two Gregorian YYYY-MM-DD strings. Returns None if either is invalid."""
    if not start_date_str or not end_date_str:
        return None
    try:
        start = datetime.strptime(str(start_date_str), '%Y-%m-%d').date()
        end = datetime.strptime(str(end_date_str), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None

    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)