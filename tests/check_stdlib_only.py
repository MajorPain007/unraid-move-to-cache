#!/usr/bin/env python3
"""Assert the daemon imports nothing outside the standard library.

Unraid's root filesystem is a tmpfs: anything installed with pip is gone after
the next reboot, so a single non-stdlib import turns the daemon into something
that works until the server restarts.

Run this under the OLDEST Python the daemon has to support. The check resolves
every import against the interpreter running it, so it also catches modules
that exist in a newer stdlib but not in an older one (tomllib, for example, is
stdlib on 3.11+ and simply absent before that).

    python3 tests/check_stdlib_only.py
"""
import argparse
import ast
import importlib.util
import pathlib
import site
import sys
import sysconfig


def top_level_imports(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    return names


def _dirs(paths):
    out = []
    for p in paths:
        if p:
            try:
                out.append(pathlib.Path(p).resolve())
            except OSError:
                pass
    return out


def stdlib_dirs():
    paths = sysconfig.get_paths()
    return _dirs([paths.get('stdlib'), paths.get('platstdlib')])


def site_dirs():
    # On Linux site-packages lives *inside* the stdlib directory, so it has to
    # be subtracted explicitly rather than tested by "is it under stdlib".
    found = []
    for getter in (getattr(site, 'getsitepackages', None),
                   getattr(site, 'getusersitepackages', None)):
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            continue
        found.extend([value] if isinstance(value, str) else value)
    return _dirs(found)


def _under(path, roots):
    return any(path == root or root in path.parents for root in roots)


def classify(name, std_dirs, pkg_dirs):
    if name in sys.builtin_module_names:
        return 'stdlib', 'built-in'
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        spec = None
    if spec is None:
        return 'missing', 'not found in this Python'

    origin = spec.origin or ''
    if origin in ('built-in', 'frozen'):
        return 'stdlib', origin
    if not origin:
        return 'external', 'namespace package'

    try:
        path = pathlib.Path(origin).resolve()
    except OSError:
        return 'external', origin
    if _under(path, pkg_dirs):
        return 'external', str(path)
    if _under(path, std_dirs):
        return 'stdlib', str(path)
    return 'external', str(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('targets', nargs='*', default=['src/plex_to_cache.py'],
                    help='Python files to check (default: the daemon)')
    args = ap.parse_args()

    std_dirs, pkg_dirs = stdlib_dirs(), site_dirs()
    version = '.'.join(str(n) for n in sys.version_info[:3])
    problems = []

    for target in args.targets:
        path = pathlib.Path(target)
        if not path.exists():
            problems.append((target, '-', 'file not found'))
            continue
        for name in sorted(top_level_imports(path)):
            verdict, where = classify(name, std_dirs, pkg_dirs)
            if verdict != 'stdlib':
                problems.append((target, name, '%s (%s)' % (verdict, where)))

    if problems:
        for target, name, detail in problems:
            sys.stderr.write('%s: %s -> %s\n' % (target, name, detail))
            print('::error file=%s::%s is not available from the standard '
                  'library of Python %s (%s)' % (target, name, version, detail))
        return 1

    print('Python %s: all imports resolve to the standard library' % version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
