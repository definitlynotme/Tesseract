"""Lazy audit instrumentation, including PyInstaller's frozen module loader."""
from __future__ import annotations

import sys


class _ObservedLoader:
    def __init__(self, loader, after_import):
        self.loader = loader
        self.after_import = after_import

    def create_module(self, spec):
        create = getattr(self.loader, 'create_module', None)
        return create(spec) if create else None

    def exec_module(self, module):
        self.loader.exec_module(module)
        self.after_import(module)

    def __getattr__(self, name):
        return getattr(self.loader, name)


class AuditImportHooks:
    def __init__(self, callbacks):
        self.callbacks = dict(callbacks)
        self.applied = set()

    def _apply(self, name, module):
        if name not in self.applied:
            self.callbacks[name](module)
            self.applied.add(name)

    def install(self):
        sys.meta_path.insert(0, self)
        for name in self.callbacks:
            module = sys.modules.get(name)
            if module is not None:
                self._apply(name, module)

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.callbacks or fullname in self.applied:
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is None or not hasattr(spec.loader, 'exec_module'):
                raise ImportError(f'audit cannot instrument module: {fullname}')
            spec.loader = _ObservedLoader(
                spec.loader, lambda module: self._apply(fullname, module),
            )
            return spec
        raise ImportError(f'audit cannot locate required module: {fullname}')

    def remove(self):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
