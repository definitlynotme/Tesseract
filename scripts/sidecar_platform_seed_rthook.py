# T-P2-H7 / T-P2-H9: frozen sidecar and frozen Qt app (runtime_hooks of both
# tesseract-runtime.spec and Tesseract.spec, after tier3_audit_runtime_hook.py). Custom runtime hooks run before PyInstaller's
# standard ones; pyi_rth_setuptools would otherwise call platform.system()
# (setuptools -> jaraco.context) and hit WMI / ``cmd /c ver`` before the entry.
# Standard library only; see sidecar_platform_seed.py.


def _h7_seed_platform():
    import sidecar_platform_seed
    sidecar_platform_seed.ensure_seeded()


_h7_seed_platform()
del _h7_seed_platform
