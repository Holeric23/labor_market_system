"""Shared search API, loaded on demand without preloading CLI modules."""
__all__ = ['Catalog', 'InvalidCursor', 'SearchFilter', 'SearchPage']


def __getattr__(name):
    if name in __all__:
        from . import service
        value = getattr(service, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
