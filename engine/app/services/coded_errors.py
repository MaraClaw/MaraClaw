"""HTTP refusals whose JSON ``detail`` is ``{code, message}``, so clients branch on a stable ``code``."""

from fastapi import HTTPException


def coded_error(status_code: int, code: str, message: str, **extra: str | int) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message, **extra})


def conflict(code: str, message: str, **extra: str | int) -> HTTPException:
    return coded_error(409, code, message, **extra)
