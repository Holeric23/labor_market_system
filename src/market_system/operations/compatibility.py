"""Resolve paths of an immutable saved model without rewriting its historical spec."""
import copy
from market_system.paths import local_path
from market_system.search.store import digest


def resolved_spec(spec):
    if spec['fingerprint'] != digest(spec['files']):
        raise ValueError('Corrupt original model fingerprint')
    result = copy.deepcopy(spec)
    result['files'] = {str(local_path(p)):v for p,v in spec['files'].items()}
    if len(result['files']) != len(spec['files']):
        raise ValueError('Relocation aliases collide')
    result['paths'] = {k:str(local_path(v)) for k,v in spec['paths'].items()}
    result['fingerprint'] = digest(result['files'])
    return result
