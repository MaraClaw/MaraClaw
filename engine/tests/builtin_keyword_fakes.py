from typing import TypedDict
from uuid import UUID


class IdKeyword(TypedDict):
    id: UUID


class SocketTypeKeyword(TypedDict):
    type: int


class IncludeAllKeyword(TypedDict):
    all: bool
