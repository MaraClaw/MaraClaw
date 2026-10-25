from fastapi.responses import JSONResponse

from app.records.company_admission import AdmissionStatus, CompanyAdmissionRecord
from app.records.company_transfer import CompanyTransferRecord
from app.records.user import UserRecord
from app.schemas.company_admission import ApprovedAdmissionResponse, PendingAdmissionResponse
from app.schemas.company_transfer import PaymentReviewResponse, TransferSummary
from app.services.company_admission_auto import submit_or_admit
from app.services.company_admissions import summarize
from app.services.company_transfer_requests import TransferDestination, TransferSelection, submit_transfer


async def admission_response(admission: CompanyAdmissionRecord) -> JSONResponse:
    """200 approved for an invitation admission that already moved the member, else 202 pending."""
    summary = await summarize(admission)
    if admission.status == AdmissionStatus.APPROVED:
        return JSONResponse(status_code=200, content=ApprovedAdmissionResponse(request=summary).model_dump(mode="json"))
    return JSONResponse(status_code=202, content=PendingAdmissionResponse(request=summary).model_dump(mode="json"))


async def transfer_response(
    applicant: UserRecord, destination: TransferDestination, selection: TransferSelection | None
) -> JSONResponse:
    result = (
        await submit_or_admit(
            applicant, destination.tenant_id, destination.method, invitation_code=destination.invitation_code
        )
        if selection is None
        else await submit_transfer(applicant, destination, selection)
    )
    if isinstance(result, CompanyTransferRecord):
        payment_response = PaymentReviewResponse(transfer_request=TransferSummary.model_validate(result))
        return JSONResponse(status_code=202, content=payment_response.model_dump(mode="json"))
    return await admission_response(result)
