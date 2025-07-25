"""Importer utilities for autodoc"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import traceback
import typing
from enum import Enum
from importlib.abc import FileLoader
from importlib.machinery import EXTENSION_SUFFIXES
from importlib.util import decode_source, find_spec, module_from_spec, spec_from_loader
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from sphinx.errors import PycodeError
from sphinx.ext.autodoc.mock import ismock, undecorate
from sphinx.pycode import ModuleAnalyzer
from sphinx.util import logging
from sphinx.util.inspect import (
    getannotations,
    getmro,
    getslots,
    isclass,
    isenumclass,
    safe_getattr,
    unwrap_all,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from importlib.machinery import ModuleSpec
    from types import ModuleType
    from typing import Any, Protocol

    from sphinx.ext.autodoc import ObjectMember

    class _AttrGetter(Protocol):
        def __call__(self, obj: Any, name: str, default: Any = ..., /) -> Any: ...


_NATIVE_SUFFIXES: frozenset[str] = frozenset({'.pyx', *EXTENSION_SUFFIXES})
logger = logging.getLogger(__name__)


def _filter_enum_dict(
    enum_class: type[Enum],
    attrgetter: _AttrGetter,
    enum_class_dict: Mapping[str, object],
) -> Iterator[tuple[str, type, Any]]:
    """Find the attributes to document of an enumeration class.

    The output consists of triplets ``(attribute name, defining class, value)``
    where the attribute name can appear more than once during the iteration
    but with different defining class. The order of occurrence is guided by
    the MRO of *enum_class*.
    """
    # attributes that were found on a mixin type or the data type
    candidate_in_mro: set[str] = set()
    # sunder names that were picked up (and thereby allowed to be redefined)
    # see: https://docs.python.org/3/howto/enum.html#supported-dunder-names
    sunder_names = {
        '_name_',
        '_value_',
        '_missing_',
        '_order_',
        '_generate_next_value_',
    }
    # attributes that can be picked up on a mixin type or the enum's data type
    public_names = {'name', 'value', *object.__dict__, *sunder_names}
    # names that are ignored by default
    ignore_names = Enum.__dict__.keys() - public_names

    def should_ignore(name: str, value: Any) -> bool:
        if name in sunder_names:
            return _is_native_enum_api(value, name)
        return name in ignore_names

    sentinel = object()

    def query(name: str, defining_class: type) -> tuple[str, type, Any] | None:
        value = attrgetter(enum_class, name, sentinel)
        if value is not sentinel:
            return name, defining_class, value
        return None

    # attributes defined on a parent type, possibly shadowed later by
    # the attributes defined directly inside the enumeration class
    for parent in enum_class.__mro__:
        if parent in {enum_class, Enum, object}:
            continue

        parent_dict = attrgetter(parent, '__dict__', {})
        for name, value in parent_dict.items():
            if should_ignore(name, value):
                continue

            candidate_in_mro.add(name)
            if (item := query(name, parent)) is not None:
                yield item

    # exclude members coming from the native Enum unless
    # they were redefined on a mixin type or the data type
    excluded_members = Enum.__dict__.keys() - candidate_in_mro
    yield from filter(
        None,
        (
            query(name, enum_class)
            for name in enum_class_dict
            if name not in excluded_members
        ),
    )

    # check if allowed members from ``Enum`` were redefined at the enum level
    special_names = sunder_names | public_names
    special_names &= enum_class_dict.keys()
    special_names &= Enum.__dict__.keys()
    for name in special_names:
        if (
            not _is_native_enum_api(enum_class_dict[name], name)
            and (item := query(name, enum_class)) is not None
        ):
            yield item


def _is_native_enum_api(obj: object, name: str) -> bool:
    """Check whether *obj* is the same as ``Enum.__dict__[name]``."""
    return unwrap_all(obj) is unwrap_all(Enum.__dict__[name])


def mangle(subject: Any, name: str) -> str:
    """Mangle the given name."""
    try:
        if isclass(subject) and name.startswith('__') and not name.endswith('__'):
            return f'_{subject.__name__}{name}'
    except AttributeError:
        pass

    return name


def unmangle(subject: Any, name: str) -> str | None:
    """Unmangle the given name."""
    try:
        if isclass(subject) and not name.endswith('__'):
            prefix = f'_{subject.__name__}__'
            if name.startswith(prefix):
                return name.replace(prefix, '__', 1)
            else:
                for cls in subject.__mro__:
                    prefix = f'_{cls.__name__}__'
                    if name.startswith(prefix):
                        # mangled attribute defined in parent class
                        return None
    except AttributeError:
        pass

    return name


def import_module(modname: str, try_reload: bool = False) -> Any:
    if modname in sys.modules:
        return sys.modules[modname]

    skip_pyi = bool(os.getenv('SPHINX_AUTODOC_IGNORE_NATIVE_MODULE_TYPE_STUBS', ''))
    original_module_names = frozenset(sys.modules)
    try:
        spec = find_spec(modname)
        if spec is None:
            msg = f'No module named {modname!r}'
            raise ModuleNotFoundError(msg, name=modname)  # NoQA: TRY301
        spec, pyi_path = _find_type_stub_spec(spec, modname)
        if skip_pyi or pyi_path is None:
            module = importlib.import_module(modname)
        else:
            if spec.loader is None:
                msg = 'missing loader'
                raise ImportError(msg, name=spec.name)  # NoQA: TRY301
            sys.modules[modname] = module = module_from_spec(spec)
            spec.loader.exec_module(module)
    except ImportError:
        raise
    except BaseException as exc:
        # Importing modules may cause any side effects, including
        # SystemExit, so we need to catch all errors.
        raise ImportError(exc, traceback.format_exc()) from exc
    if try_reload and os.environ.get('SPHINX_AUTODOC_RELOAD_MODULES'):
        new_modules = [m for m in sys.modules if m not in original_module_names]
        # Try reloading modules with ``typing.TYPE_CHECKING == True``.
        try:
            typing.TYPE_CHECKING = True  # type: ignore[misc]
            # Ignore failures; we've already successfully loaded these modules
            with contextlib.suppress(ImportError, KeyError):
                for m in new_modules:
                    mod_path = getattr(sys.modules[m], '__file__', '')
                    if mod_path and mod_path.endswith('.pyi'):
                        continue
                    _reload_module(sys.modules[m])
        finally:
            typing.TYPE_CHECKING = False  # type: ignore[misc]
        module = sys.modules[modname]
    return module


def _find_type_stub_spec(
    spec: ModuleSpec, modname: str
) -> tuple[ModuleSpec, Path | None]:
    """Try finding a spec for a PEP 561 '.pyi' stub file for native modules."""
    if spec.origin is None:
        return spec, None

    for suffix in _NATIVE_SUFFIXES:
        if not spec.origin.endswith(suffix):
            continue
        pyi_path = Path(spec.origin.removesuffix(suffix) + '.pyi')
        if not pyi_path.is_file():
            continue
        pyi_loader = _StubFileLoader(modname, path=str(pyi_path))
        pyi_spec = spec_from_loader(modname, loader=pyi_loader)
        if pyi_spec is not None:
            return pyi_spec, pyi_path
    return spec, None


class _StubFileLoader(FileLoader):
    """Load modules from ``.pyi`` stub files."""

    def get_source(self, fullname: str) -> str:
        path = self.get_filename(fullname)
        for suffix in _NATIVE_SUFFIXES:
            if not path.endswith(suffix):
                continue
            path = path.removesuffix(suffix) + '.pyi'
        try:
            source_bytes = self.get_data(path)
        except OSError as exc:
            raise ImportError from exc
        return decode_source(source_bytes)


def _reload_module(module: ModuleType) -> Any:
    """Call importlib.reload(module), convert exceptions to ImportError"""
    try:
        return importlib.reload(module)
    except BaseException as exc:
        # Importing modules may cause any side effects, including
        # SystemExit, so we need to catch all errors.
        raise ImportError(exc, traceback.format_exc()) from exc


def import_object(
    modname: str,
    objpath: list[str],
    objtype: str = '',
    attrgetter: _AttrGetter = safe_getattr,
) -> Any:
    if objpath:
        logger.debug('[autodoc] from %s import %s', modname, '.'.join(objpath))
    else:
        logger.debug('[autodoc] import %s', modname)

    try:
        module = None
        exc_on_importing = None
        objpath = objpath.copy()
        while module is None:
            try:
                module = import_module(modname, try_reload=True)
                logger.debug('[autodoc] import %s => %r', modname, module)
            except ImportError as exc:
                logger.debug('[autodoc] import %s => failed', modname)
                exc_on_importing = exc
                if '.' in modname:
                    # retry with parent module
                    modname, name = modname.rsplit('.', 1)
                    objpath.insert(0, name)
                else:
                    raise

        obj = module
        parent = None
        object_name = None
        for attrname in objpath:
            parent = obj
            logger.debug('[autodoc] getattr(_, %r)', attrname)
            mangled_name = mangle(obj, attrname)
            obj = attrgetter(obj, mangled_name)

            try:
                logger.debug('[autodoc] => %r', obj)
            except TypeError:
                # fallback of failure on logging for broken object
                # See: https://github.com/sphinx-doc/sphinx/issues/9095
                logger.debug('[autodoc] => %r', (obj,))

            object_name = attrname
        return [module, parent, object_name, obj]
    except (AttributeError, ImportError) as exc:
        if isinstance(exc, AttributeError) and exc_on_importing:
            # restore ImportError
            exc = exc_on_importing

        if objpath:
            errmsg = 'autodoc: failed to import %s %r from module %r' % (
                objtype,
                '.'.join(objpath),
                modname,
            )
        else:
            errmsg = f'autodoc: failed to import {objtype} {modname!r}'

        if isinstance(exc, ImportError):
            # import_module() raises ImportError having real exception obj and
            # traceback
            real_exc = exc.args[0]
            traceback_msg = traceback.format_exception(exc)
            if isinstance(real_exc, SystemExit):
                errmsg += (
                    '; the module executes module level statement '
                    'and it might call sys.exit().'
                )
            elif isinstance(real_exc, ImportError) and real_exc.args:
                errmsg += '; the following exception was raised:\n%s' % real_exc.args[0]
            else:
                errmsg += '; the following exception was raised:\n%s' % traceback_msg
        else:
            errmsg += (
                '; the following exception was raised:\n%s' % traceback.format_exc()
            )

        logger.debug(errmsg)
        raise ImportError(errmsg) from exc


class Attribute(NamedTuple):
    name: str
    directly_defined: bool
    value: Any


def get_object_members(
    subject: Any,
    objpath: list[str],
    attrgetter: _AttrGetter,
    analyzer: ModuleAnalyzer | None = None,
) -> dict[str, Attribute]:
    """Get members and attributes of target object."""
    from sphinx.ext.autodoc._sentinels import INSTANCE_ATTR

    # the members directly defined in the class
    obj_dict = attrgetter(subject, '__dict__', {})

    members: dict[str, Attribute] = {}

    # enum members
    if isenumclass(subject):
        for name, defining_class, value in _filter_enum_dict(
            subject, attrgetter, obj_dict
        ):
            # the order of occurrence of *name* matches the subject's MRO,
            # allowing inherited attributes to be shadowed correctly
            if unmangled := unmangle(defining_class, name):
                members[unmangled] = Attribute(
                    name=unmangled,
                    directly_defined=defining_class is subject,
                    value=value,
                )

    # members in __slots__
    try:
        subject___slots__ = getslots(subject)
        if subject___slots__:
            from sphinx.ext.autodoc._sentinels import SLOTS_ATTR

            for name in subject___slots__:
                members[name] = Attribute(
                    name=name, directly_defined=True, value=SLOTS_ATTR
                )
    except (TypeError, ValueError):
        pass

    # other members
    for name in dir(subject):
        try:
            value = attrgetter(subject, name)
            directly_defined = name in obj_dict
            unmangled = unmangle(subject, name)
            if unmangled and unmangled not in members:
                members[unmangled] = Attribute(
                    name=unmangled, directly_defined=directly_defined, value=value
                )
        except AttributeError:
            continue

    # annotation only member (ex. attr: int)
    for cls in getmro(subject):
        for name in getannotations(cls):
            unmangled = unmangle(cls, name)
            if unmangled and unmangled not in members:
                members[unmangled] = Attribute(
                    name=unmangled, directly_defined=cls is subject, value=INSTANCE_ATTR
                )

    if analyzer:
        # append instance attributes (cf. self.attr1) if analyzer knows
        namespace = '.'.join(objpath)
        for ns, name in analyzer.find_attr_docs():
            if namespace == ns and name not in members:
                members[name] = Attribute(
                    name=name, directly_defined=True, value=INSTANCE_ATTR
                )

    return members


def get_class_members(
    subject: Any, objpath: Any, attrgetter: _AttrGetter, inherit_docstrings: bool = True
) -> dict[str, ObjectMember]:
    """Get members and attributes of target class."""
    from sphinx.ext.autodoc._documenters import ObjectMember
    from sphinx.ext.autodoc._sentinels import INSTANCE_ATTR

    # the members directly defined in the class
    obj_dict = attrgetter(subject, '__dict__', {})

    members: dict[str, ObjectMember] = {}

    # enum members
    if isenumclass(subject):
        for name, defining_class, value in _filter_enum_dict(
            subject, attrgetter, obj_dict
        ):
            # the order of occurrence of *name* matches the subject's MRO,
            # allowing inherited attributes to be shadowed correctly
            if unmangled := unmangle(defining_class, name):
                members[unmangled] = ObjectMember(
                    unmangled, value, class_=defining_class
                )

    # members in __slots__
    try:
        subject___slots__ = getslots(subject)
        if subject___slots__:
            from sphinx.ext.autodoc._sentinels import SLOTS_ATTR

            for name, docstring in subject___slots__.items():
                members[name] = ObjectMember(
                    name, SLOTS_ATTR, class_=subject, docstring=docstring
                )
    except (TypeError, ValueError):
        pass

    # other members
    for name in dir(subject):
        try:
            value = attrgetter(subject, name)
            if ismock(value):
                value = undecorate(value)

            unmangled = unmangle(subject, name)
            if unmangled and unmangled not in members:
                if name in obj_dict:
                    members[unmangled] = ObjectMember(unmangled, value, class_=subject)
                else:
                    members[unmangled] = ObjectMember(unmangled, value)
        except AttributeError:
            continue

    try:
        for cls in getmro(subject):
            try:
                modname = safe_getattr(cls, '__module__')
                qualname = safe_getattr(cls, '__qualname__')
                analyzer = ModuleAnalyzer.for_module(modname)
                analyzer.analyze()
            except AttributeError:
                qualname = None
                analyzer = None
            except PycodeError:
                analyzer = None

            # annotation only member (ex. attr: int)
            for name in getannotations(cls):
                unmangled = unmangle(cls, name)
                if unmangled and unmangled not in members:
                    if analyzer and (qualname, unmangled) in analyzer.attr_docs:
                        docstring = '\n'.join(analyzer.attr_docs[qualname, unmangled])
                    else:
                        docstring = None

                    members[unmangled] = ObjectMember(
                        unmangled, INSTANCE_ATTR, class_=cls, docstring=docstring
                    )

            # append or complete instance attributes (cf. self.attr1) if analyzer knows
            if analyzer:
                for (ns, name), docstring in analyzer.attr_docs.items():
                    if ns == qualname and name not in members:
                        # otherwise unknown instance attribute
                        members[name] = ObjectMember(
                            name,
                            INSTANCE_ATTR,
                            class_=cls,
                            docstring='\n'.join(docstring),
                        )
                    elif (
                        ns == qualname
                        and docstring
                        and isinstance(members[name], ObjectMember)
                        and not members[name].docstring
                    ):
                        if cls != subject and not inherit_docstrings:
                            # If we are in the MRO of the class and not the class itself,
                            # and we do not want to inherit docstrings, then skip setting
                            # the docstring below
                            continue
                        # attribute is already known, because dir(subject) enumerates it.
                        # But it has no docstring yet
                        members[name].docstring = '\n'.join(docstring)
    except AttributeError:
        pass

    return members
