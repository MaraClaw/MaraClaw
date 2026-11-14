"""Payment evidence of organization admission requests: the public multipart submit and the reviewer's download.

Kept out of ``scripts.api_contract_registry.AUDITED_HTTP_MODULES`` on purpose: the multipart body schema FastAPI derives
for the submit has no public name the Zod generator accepts (``capacity_evidence`` is unaudited for the same reason).
The acknowledgment it returns is registered explicitly in ``explicit_schemas`` so the frontends still get a validator.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.organization_admission import OrganizationAdmissionAcknowledgment
from app.services.capacity_evidence import MAX_EVIDENCE_BYTES, evidence_response
from app.services.document_parser.errors import DocumentTooLargeError
from app.services.document_parser.service import read_bytes_limited
from app.services.organization_admissions import read_evidence, submit_request, verify_submission_token

router = APIRouter(tags=["organization-admissions"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


@router.post(
    "/organization-admissions",
    response_model=OrganizationAdmissionAcknowledgment,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_organization_admission(
    request: Request, quote_token: str = Form(...), file: UploadFile = File(...)
) -> OrganizationAdmissionAcknowledgment:
    """Submit the request a quote priced, with the bank-transfer evidence (image or PDF, at most 10 MiB)."""
    terms = verify_submission_token(quote_token)
    try:
        data = await read_bytes_limited(file.read, MAX_EVIDENCE_BYTES)
    except DocumentTooLargeError:
        raise HTTPException(413, "Payment evidence must be 10 MiB or smaller") from None
    reference = await submit_request(terms, data=data, filename=file.filename, client_ip=_client_ip(request))
    return OrganizationAdmissionAcknowledgment(status="pending", reference=reference)


@router.get("/admin/organization-admissions/{request_id}/evidence")
async def download_organization_admission_evidence(
    request_id: UUID, user: UserRecord = Depends(get_current_user)
) -> Response:
    """The stored evidence with its sniffed type; genesis platform admin only, sandboxed and never sniffed again."""
    return evidence_response(await read_evidence(user.id, request_id))
