#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import zlib
import logging
import subprocess
import urllib.parse
from typing import Union, Iterable

logger = logging.getLogger(__name__)


def has_binary(name):
    """whether system has this binary in PATH"""
    return (
        subprocess.run(
            ["/usr/bin/env", "which", name], stdout=subprocess.DEVNULL
        ).returncode
        == 0
    )


def get_short_hash(text: str) -> str:
    letters = ["E", "T", "A", "I", "N", "O", "S", "H", "R", "D"]
    return "".join([letters[int(x)] for x in str(zlib.adler32(text.encode("UTF-8")))])


def first(*args: Iterable[object]) -> object:
    """first non-None value from *args ; fallback to empty string"""
    return next((item for item in args if item is not None), "")


def rebuild_uri(
    uri: urllib.parse.ParseResult,
    scheme: str = None,
    username: str = None,
    password: str = None,
    hostname: str = None,
    port: Union[str, int] = None,
    path: str = None,
    params: str = None,
    query: str = None,
    fragment: str = None,
) -> urllib.parse.ParseResult:
    """new named tuple from uri with request part updated"""
    username = first(username, uri.username, "")
    password = first(password, uri.password, "")
    hostname = first(hostname, uri.hostname, "")
    port = first(port, uri.port, "")
    netloc = (
        f"{username}{':' if password else ''}{password}"
        f"{'@' if username or password else ''}{hostname}"
        f"{':' if port else ''}{port}"
    )
    return urllib.parse.urlparse(
        urllib.parse.urlunparse(
            (
                first(scheme, uri.scheme),
                netloc,
                first(path, uri.path),
                first(params, uri.params),
                first(query, uri.query),
                first(fragment, uri.fragment),
            )
        )
    )
