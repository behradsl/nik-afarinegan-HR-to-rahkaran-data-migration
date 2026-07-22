# utils/data_helpers.py
import pandas as pd
import re

# Invisible / formatting characters that break keyboard search
_INVISIBLE_CHARS_RE = re.compile(
    r'[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]'
)
# Arabic diacritics (tashkeel) and Quranic marks
_ARABIC_DIACRITICS_RE = re.compile(
    r'[\u064b-\u065f\u0670\u06d6-\u06ed]'
)


def clean_value(val):
    """Converts NaNs, '0', 0, and empty strings to None (SQL NULL)."""
    if pd.isna(val):
        return None
    val_str = str(val).strip()
    if val_str in ('', '0', '0.0', 'None'):
        return None
    return val


def normalize_persian(text):
    """
    Standardize Persian text for keyboard search compatibility.

    Maps Arabic lookalikes to Persian keyboard characters (ی/ک),
    strips diacritics and zero-width marks, collapses whitespace.
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return text
    if not isinstance(text, str):
        text = str(text)

    # Arabic Yeh / Alef Maksura → Persian Yeh (keyboard ی)
    text = text.replace('\u064a', '\u06cc')  # ي → ی
    text = text.replace('\u0649', '\u06cc')  # ى → ی
    # Arabic Kaf → Persian Keheh (keyboard ک)
    text = text.replace('\u0643', '\u06a9')  # ك → ک

    # Common hamza-on-alef forms → plain Alef (search-friendly)
    text = text.replace('\u0623', '\u0627')  # أ → ا
    text = text.replace('\u0625', '\u0627')  # إ → ا
    text = text.replace('\u0622', '\u0627')  # آ → ا
    text = text.replace('\u0671', '\u0627')  # ٱ → ا

    # Teh Marbuta → Heh
    text = text.replace('\u0629', '\u0647')  # ة → ه

    # Arabic-Indic digits → ASCII (optional consistency for mixed fields)
    arabic_indic = '٠١٢٣٤٥٦٧٨٩'
    persian_digits = '۰۱۲۳۴۵۶۷۸۹'
    for i, ch in enumerate(arabic_indic):
        text = text.replace(ch, str(i))
    for i, ch in enumerate(persian_digits):
        text = text.replace(ch, str(i))

    text = _ARABIC_DIACRITICS_RE.sub('', text)
    text = _INVISIBLE_CHARS_RE.sub('', text)

    # Collapse whitespace (including non-breaking spaces)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = ' '.join(text.split())
    return text


def clean_persian_text(val):
    """
    clean_value + normalize_persian for Persian name/title fields.
    Returns None when empty after cleaning.
    """
    cleaned = clean_value(val)
    if cleaned is None:
        return None
    text = normalize_persian(str(cleaned).strip())
    if not text:
        return None
    return text
