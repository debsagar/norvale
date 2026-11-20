"""Validate native numeric Quality Inspection readings."""
from decimal import Decimal, InvalidOperation


def numeric_results(doc):
    """Return comparable native numeric assay results, keyed by method and limits."""
    if doc.get('manual_inspection'):
        return {}
    result = {}
    for row in doc.get('readings', []):
        if (row.get('numeric') != 1 or not row.get('specification') or row.get('manual_inspection')
                or row.get('formula_based_criteria')):
            return {}
        try:
            low, high, value = (Decimal(str(row.get(k))) for k in ('min_value', 'max_value', 'reading_1'))
        except InvalidOperation:
            return {}
        if not all(x.is_finite() for x in (low, high, value)) or high < low:
            return {}
        method = row['specification'], low, high
        if method in result:
            return {}
        within_limits = low <= value <= high
        if row.get('status') != ('Accepted' if within_limits else 'Rejected'):
            return {}
        result[method] = within_limits
    return result
