from . import inline_keyboards as _inline_keyboards

__all__ = [name for name in dir(_inline_keyboards) if not name.startswith("_")]

globals().update({name: getattr(_inline_keyboards, name) for name in __all__})
