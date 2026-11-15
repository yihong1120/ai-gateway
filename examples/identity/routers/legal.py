from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from examples.identity.database import get_db
from examples.identity.schemas.legal import LegalDocumentOut
from examples.identity.schemas.legal import LegalDocumentsResponse
from examples.identity.accounts.legal import DEFAULT_LEGAL_LOCALE
from examples.identity.accounts.legal import get_active_legal_documents

router = APIRouter(prefix='/legal', tags=['legal'])


@router.get('/documents', response_model=LegalDocumentsResponse)
async def get_legal_documents(
    locale: str = Query(default=DEFAULT_LEGAL_LOCALE),
    db: AsyncSession = Depends(get_db),
) -> LegalDocumentsResponse:
    """Return legal documents currently required for signup consent.

    Args:
        locale: Preferred locale for legal-document content.
        db: Database session used to load active documents.

    Returns:
        Current terms, privacy notice, and AI-specific terms.

    Raises:
        HTTPException: If a required document is unavailable in the requested
            locale or its configured fallback.
    """
    docs = await get_active_legal_documents(db, locale)
    return LegalDocumentsResponse(
        terms=LegalDocumentOut.model_validate(docs['terms']),
        privacy=LegalDocumentOut.model_validate(docs['privacy']),
        ai_terms=LegalDocumentOut.model_validate(docs['ai_terms']),
    )
