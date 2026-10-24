from fastapi.responses import JSONResponse

from app.records.company_transfer import CompanyTransferRecord
from app.records.user import UserRecord
from app.schemas.company_admission import PendingAdmissionResponse
from app.schemas.company_transfer import PaymentReviewResponse, TransferSummary
from app.services.company_admissions import submit_request, summarize
from app.services.company_transfer_requests import TransferDestination, TransferSelection, submit_transfer


async def transfer_response(
    applicant: UserRecord, destination: TransferDestination, selection: TransferSelection | None
) -> JSONResponse:
    result = (
        await submit_request(
            applicant, destination.tenant_id, destination.method, invitation_code=destination.invitation_code
        )
        if selection is None
        else await submit_transfer(applicant, destination, selection)
    )
    if isinstance(result, CompanyTransferRecord):
        payment_response = PaymentReviewResponse(transfer_request=TransferSummary.model_validate(result))
        return JSONResponse(status_code=202, content=payment_response.model_dump(mode="json"))
    response = PendingAdmissionResponse(request=await summarize(result))
    return JSONResponse(status_code=202, content=response.model_dump(mode="json"))
