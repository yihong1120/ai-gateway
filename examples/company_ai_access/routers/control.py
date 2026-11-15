"""Browser approval and native device lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from examples.company_ai_access import schemas
from examples.company_ai_access.schemas import Start, Proof, Approve
from examples.company_ai_access.routers.dependencies import service, mutation, client_ip, require_inference_network
from examples.company_ai_access.errors import error


def create_router(principal_dependency):
    router = APIRouter(
        prefix='/api/company-ai',
        tags=['company-ai-access'],
        dependencies=[Depends(require_inference_network)],
        responses={
            status: {'model': schemas.ControlError, 'description': description}
            for status, description in {
                401: 'Login or device proof required',
                403: 'Access denied',
                404: 'Not found or not owned',
                409: 'State conflict or device limit',
                410: 'Enrollment expired or consumed',
                422: 'Invalid request',
                429: 'Rate limited',
                503: 'Dependency unavailable',
            }.items()
        },
    )

    async def member(request: Request, principal=Depends(principal_dependency)):
        await service(request).member(principal.subject)
        return principal

    @router.get('/policy', response_model=schemas.Policy)
    async def policy(request: Request, principal=Depends(member)):
        return service(request).policy()

    @router.get('/devices', response_model=schemas.DeviceList, response_model_exclude_none=True)
    async def devices(request: Request, principal=Depends(member)):
        return {'devices': (await service(request).devices(principal.subject))}

    @router.post(
        '/devices/{device_id}/revoke',
        dependencies=[Depends(mutation)],
        response_model=schemas.Revoked,
    )
    async def revoke(device_id: str, request: Request, principal=Depends(member)):
        return await service(request).revoke(principal.subject, device_id)

    @router.get(
        '/admin/devices',
        response_model=schemas.DeviceList,
        response_model_exclude_none=True,
    )
    async def admin_devices(request: Request, principal=Depends(member)):
        if not principal.admin or not await service(request).directory.is_admin(principal.subject):
            raise error(403, 'ai_access_admin_required')
        return {'devices': (await service(request).devices())}

    @router.post(
        '/admin/devices/{device_id}/revoke',
        dependencies=[Depends(mutation)],
        response_model=schemas.Revoked,
    )
    async def admin_revoke(device_id: str, request: Request, principal=Depends(member)):
        if not principal.admin or not await service(request).directory.is_admin(principal.subject):
            raise error(403, 'ai_access_admin_required')
        return await service(request).revoke(principal.subject, device_id, admin=True)

    @router.get('/enrollments/{request_id}', response_model=schemas.EnrollmentPreview)
    async def preview(request_id: str, request: Request, principal=Depends(member)):
        return await service(request).preview(principal.subject, request_id)

    @router.post(
        '/enrollments/{request_id}/approve',
        dependencies=[Depends(mutation)],
        response_model=schemas.Approved,
    )
    async def approve(request_id: str, form: Approve, request: Request, principal=Depends(member)):
        return await service(request).approve(principal.subject, request_id, form.user_code)

    # Native device flow: no browser cookie is accepted as authority here.
    # PKCE possession plus authenticated, CSRF-protected browser approval is
    # required before a key can be delivered to the initiating terminal.
    @router.post('/device/start', response_model=schemas.Started)
    async def start(form: Start, request: Request):
        if request.headers.get('origin') is not None:
            raise error(403, 'ai_access_native_client_required')
        return await service(request).start(
            form.device_name,
            form.code_challenge,
            form.replace_device_id,
            client_ip(request),
        )

    @router.post('/device/poll', response_model=schemas.Pending | schemas.Ready)
    async def poll(form: Proof, request: Request):
        state = service(request)
        await state.throttle('proof', client_ip(request), 100, 60)
        return await state.poll(form.request_id, form.code_verifier)

    @router.post('/device/ack', response_model=schemas.Completed)
    async def ack(form: Proof, request: Request):
        return await service(request).ack(form.request_id, form.code_verifier)

    return router
