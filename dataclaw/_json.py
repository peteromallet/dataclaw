"""Small orjson compatibility layer for the project."""

from __future__ import annotations

from typing import IO, Any

import orjson

JSONDecodeError = orjson.JSONDecodeError


def _dump_option(indent: int | None) -> int:
    if indent is None:
        return 0
    if indent != 2:
        raise TypeError("Only indent=2 is supported")
    return orjson.OPT_INDENT_2


def dumps_bytes(obj: Any, *, indent: int | None = None) -> bytes:
    return orjson.dumps(obj, option=_dump_option(indent))


def dumps(obj: Any, *, indent: int | None = None, ensure_ascii: bool = False) -> str:
    if ensure_ascii:
        raise TypeError("ensure_ascii=True is not supported")
    return dumps_bytes(obj, indent=indent).decode("utf-8")


def dump(obj: Any, fp: IO[str], *, indent: int | None = None, ensure_ascii: bool = False) -> None:
    fp.write(dumps(obj, indent=indent, ensure_ascii=ensure_ascii))


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    return orjson.loads(data)


def load(fp: IO[str] | IO[bytes]) -> Any:
    return loads(fp.read())
