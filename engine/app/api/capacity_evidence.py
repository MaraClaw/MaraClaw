"""Payment evidence of capacity orders: the upload that finishes an order and its authorized download.

Kept out of ``scripts.api_contract_registry.AUDITED_HTTP_MODULES`` on purpose: the multipart body schema FastAPI derives
for the upload has no public name the Zod generator accepts (the other upload routes are unaudited for the same reason).
The order it returns is the audited ``CapacityOrderOut``.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.capacity import CapacityOrderOut
from app.services.capacity_evidence import MAX_EVIDENCE_BYTES, content_disposition
from app.services.capacity_orders import read_evidence, submit_evidence
from app.services.document_parser.errors import DocumentTooLargeError
from app.services.document_parser.service import read_bytes_limited

router = APIRouter(prefix="/capacity", tags=["capacity"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


@router.post("/orders/{order_id}/evidence", response_model=CapacityOrderOut)
async def upload_capacity_evidence(
    order_id: UUID, request: Request, file: UploadFile = File(...), user: UserRecord = Depends(get_current_user)
) -> CapacityOrderOut:
    """Attach the bank-transfer evidence (image or PDF, at most 10 MiB) and send the order for review."""
    try:
        data = await read_bytes_limited(file.read, MAX_EVIDENCE_BYTES)
    except DocumentTooLargeError:
        raise HTTPException(413, "Payment evidence must be 10 MiB or smaller") from None
    view = await submit_evidence(user.id, order_id, data=data, filename=file.filename, client_ip=_client_ip(request))
    return CapacityOrderOut.from_record(view.order, view.tenant_name)


@router.get("/orders/{order_id}/evidence")
async def download_capacity_evidence(order_id: UUID, user: UserRecord = Depends(get_current_user)) -> Response:
    """The stored evidence with its sniffed type; sandboxed and never sniffed again by the browser."""
    evidence = await read_evidence(user.id, order_id)
    return Response(
        evidence.data,
        media_type=evidence.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Content-Disposition": content_disposition(evidence.filename),
            "Cache-Control": "private, no-store",
        },
    )
