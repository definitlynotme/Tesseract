from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


EXPECTED_PYTHON = (3, 12, 7)
ALLOWED_BOOTSTRAP_DISTRIBUTIONS = frozenset({"pip"})
LOCK_HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def locked_requirements(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or LOCK_HASH_RE.fullmatch(parts[1]) is None:
            raise ValueError(
                "release lock entry must contain exactly one lowercase SHA-256 wheel hash: "
                f"{line}"
            )
        requirement = Requirement(parts[0])
        if requirement.url or requirement.marker or requirement.extras:
            raise ValueError(
                f"release lock entry must be a plain pinned distribution: {line}"
            )
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==":
            raise ValueError(f"release lock entry must use one exact version: {line}")
        canonical_name = canonicalize_name(requirement.name)
        if canonical_name in locked:
            raise ValueError(f"duplicate release lock entry: {canonical_name}")
        locked[canonical_name] = specs[0].version
    if not locked:
        raise ValueError(f"release lock is empty: {path}")
    return locked


def direct_requirement_names(
    paths: Iterable[Path],
    locked: dict[str, str],
) -> set[str]:
    names: set[str] = set()
    for path in paths:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.url or requirement.marker:
                raise ValueError(
                    f"release root requirement must be a plain pinned distribution: {line}"
                )
            specs = list(requirement.specifier)
            if len(specs) != 1 or specs[0].operator != "==":
                raise ValueError(
                    f"release root requirement must use one exact version: {line}"
                )
            canonical_name = canonicalize_name(requirement.name)
            locked_version = locked.get(canonical_name)
            if locked_version is None:
                raise RuntimeError(
                    f"direct requirement is missing from release lock: {canonical_name}"
                )
            if locked_version != specs[0].version:
                raise RuntimeError(
                    f"direct requirement version drifted for {canonical_name}: "
                    f"{specs[0].version} != {locked_version}"
                )
            names.add(canonical_name)
    return names


def locked_dependency_closure(
    locked: dict[str, str],
    roots: Iterable[str],
    *,
    distribution_provider: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> set[str]:
    environment = default_environment()
    environment["extra"] = ""
    pending = list(dict.fromkeys(canonicalize_name(name) for name in roots))
    closure: set[str] = set()
    while pending:
        canonical_name = pending.pop(0)
        if canonical_name in closure:
            continue
        if canonical_name not in locked:
            raise RuntimeError(
                f"release dependency is missing from lock: {canonical_name}"
            )
        try:
            distribution = distribution_provider(canonical_name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"locked distribution is not installed: {canonical_name}"
            ) from exc
        closure.add(canonical_name)
        for raw_dependency in distribution.requires or ():
            dependency = Requirement(raw_dependency)
            if dependency.marker and not dependency.marker.evaluate(environment):
                continue
            if dependency.url:
                raise RuntimeError(
                    f"locked distribution {canonical_name} has an unpinned URL "
                    f"dependency: {raw_dependency}"
                )
            dependency_name = canonicalize_name(dependency.name)
            dependency_version = locked.get(dependency_name)
            if dependency_version is None:
                raise RuntimeError(
                    f"dependency of {canonical_name} is missing from release lock: "
                    f"{dependency_name}"
                )
            if (
                dependency.specifier
                and not dependency.specifier.contains(
                    dependency_version,
                    prereleases=True,
                )
            ):
                raise RuntimeError(
                    f"locked {dependency_name}=={dependency_version} does not satisfy "
                    f"{canonical_name} requirement {dependency.specifier}"
                )
            if dependency_name not in closure:
                pending.append(dependency_name)
    return closure


def verify_lock_closure(
    lock_path: Path,
    requirement_paths: Iterable[Path],
    *,
    distribution_provider: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> set[str]:
    locked = locked_requirements(lock_path)
    roots = direct_requirement_names(requirement_paths, locked)
    closure = locked_dependency_closure(
        locked,
        roots,
        distribution_provider=distribution_provider,
    )
    unused = sorted(set(locked) - closure)
    if unused:
        raise RuntimeError(
            "release lock contains unreachable distributions: " + ", ".join(unused)
        )
    return closure


def installed_distributions() -> dict[str, str]:
    return {
        canonicalize_name(distribution.metadata["Name"]): distribution.version
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }


def verify(
    lock_path: Path,
    requirement_paths: Iterable[Path] | None = None,
) -> None:
    if sys.version_info[:3] != EXPECTED_PYTHON:
        raise RuntimeError(
            f"release Python must be {'.'.join(map(str, EXPECTED_PYTHON))}; "
            f"got {sys.version.split()[0]}"
        )
    locked = locked_requirements(lock_path)
    installed = installed_distributions()
    missing = sorted(name for name in locked if name not in installed)
    mismatched = sorted(
        f"{name}: expected {version}, got {installed[name]}"
        for name, version in locked.items()
        if name in installed and installed[name] != version
    )
    extras = sorted(set(installed) - set(locked) - ALLOWED_BOOTSTRAP_DISTRIBUTIONS)
    errors: list[str] = []
    if missing:
        errors.append("missing distributions: " + ", ".join(missing))
    if mismatched:
        errors.append("version mismatches: " + "; ".join(mismatched))
    if extras:
        errors.append("undeclared distributions: " + ", ".join(extras))
    if errors:
        raise RuntimeError("\n".join(errors))
    if requirement_paths is None:
        requirement_paths = (
            lock_path.parent / "requirements.txt",
            lock_path.parent / "requirements-build.txt",
        )
    verify_lock_closure(lock_path, requirement_paths)
    print(
        f"Release environment verified: Python {sys.version.split()[0]}, "
        f"{len(locked)} locked distributions, exact dependency closure, "
        "no undeclared distributions."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements-windows.lock.txt"),
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        nargs="+",
        default=(Path("requirements.txt"), Path("requirements-build.txt")),
    )
    args = parser.parse_args()
    verify(
        args.lock.resolve(),
        tuple(path.resolve() for path in args.requirements),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
