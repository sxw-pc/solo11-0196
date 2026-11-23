"""One auditable decision representation for every business rule."""


def make_decision(result, reason, evidence):
    return {'result': result, 'reason': reason, 'evidence': list(evidence)}
