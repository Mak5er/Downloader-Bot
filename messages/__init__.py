from . import admin_messages as _admin_messages
from . import user_messages as _user_messages

_exported_names = {
    name for name in dir(_admin_messages) if not name.startswith("_")
}
_exported_names.update(
    name for name in dir(_user_messages) if not name.startswith("_")
)
__all__ = sorted(_exported_names)

globals().update({name: getattr(_admin_messages, name) for name in dir(_admin_messages) if name in __all__})
globals().update({name: getattr(_user_messages, name) for name in dir(_user_messages) if name in __all__})
