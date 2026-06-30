from fastapi import APIRouter, Depends, HTTPException

from config import settings
from dependencies import get_current_user
from models import User
from schemas import UnitApplicationFormPrefillResponse
from services import unit as unit_svc

router = APIRouter(prefix="/unit", tags=["unit"])


def _split_name(full_name: str) -> tuple[str, str]:
    first, *rest = full_name.strip().split()
    return first, (" ".join(rest) if rest else "")


@router.get("/application-form-prefill", response_model=UnitApplicationFormPrefillResponse)
def application_form_prefill(current_user: User = Depends(get_current_user)):
    """End-user config endpoint for Unit's hosted/white-label application form.

    Configure this URL in Unit's white-label app setup. Unit calls it with the
    user token we issue for FAWN, and receives non-sensitive prefill data plus
    a stable external application id. SSN and other KYC-only fields stay inside
    Unit's hosted form instead of being collected by FAWN.
    """
    first_name, last_name = _split_name(current_user.full_name)

    return {
        "data": {
            "type": "whiteLabelAppEndUserConfig",
            "attributes": {
                "applicationFormPrefill": {
                    "fullName": {
                        "first": first_name,
                        "last": last_name or "Unknown",
                    },
                    "email": current_user.email,
                    "phone": {
                        "countryCode": "1",
                        "number": current_user.phone or "",
                    },
                    "occupation": "Student" if current_user.is_student else "",
                },
                "applicationFormSettingsOverride": {
                    "idempotencyKey": f"fawn-user-{current_user.id}",
                    "tags": {
                        "fawnUserId": current_user.id,
                        "school": current_user.school or "",
                        "militaryStatus": current_user.military_status or "",
                    },
                },
            },
        }
    }


def _extract_application_form_url(form: dict) -> str:
    links = form.get("links", {})
    for key in ("related", "self", "applicationForm"):
        value = links.get(key)
        if isinstance(value, dict) and value.get("href"):
            return value["href"]
        if isinstance(value, str):
            return value
    attrs = form.get("attributes", {})
    for key in ("url", "applicationFormUrl", "applicationUrl"):
        if attrs.get(key):
            return attrs[key]
    return ""


@router.post("/application-form")
async def create_application_form(current_user: User = Depends(get_current_user)):
    """Create a Unit-hosted KYC application form for the current user."""
    if current_user.unit_account_id:
        raise HTTPException(status_code=409, detail="Bank account is already active.")
    if settings.unit_api_token in ("UNIT_TOKEN_NOT_SET", ""):
        raise HTTPException(status_code=503, detail="Unit API token is not configured.")

    try:
        form = await unit_svc.create_application_form(
            user_id=current_user.id,
            full_name=current_user.full_name,
            email=current_user.email,
            phone=current_user.phone or "",
            is_student=bool(current_user.is_student),
            school=current_user.school,
            military_status=current_user.military_status,
        )
    except Exception as e:
        print(f"[Unit] application form creation failed: {e}")
        raise HTTPException(status_code=502, detail="Could not start Unit application form.")

    form_url = _extract_application_form_url(form)
    if not form_url:
        raise HTTPException(status_code=502, detail="Unit did not return an application form URL.")

    return {
        "application_form_id": form.get("id"),
        "application_form_url": form_url,
    }
