"""Bounded, untrusted browser diagnostics; never accepts client identity or bodies."""
import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.api.deps import get_current_identity
from app.core.config import settings
from app.core.diagnostics import event
from app.core.rate_limit import RateLimitByIP
from app.db.mongodb import get_database

router = APIRouter(tags=['diagnostics'])


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event: Literal['api.request','ui.action','ui.navigation','client.error','client.dropped']
    page: str = Field(default='', max_length=300)
    path: str = Field(default='', max_length=300)
    method: Literal['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'] | None = None
    request_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9._:-]{1,80}$')
    status: int | None = Field(default=None, ge=0, le=599)
    duration_ms: float | None = Field(default=None, ge=0, le=86400000, allow_inf_nan=False)
    error_type: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.-]{1,80}$')
    action: Literal['button.click','submit','navigation'] | None = None
    line: int | None = Field(default=None, ge=0, le=10000000)
    column: int | None = Field(default=None, ge=0, le=10000000)
    dropped_count: int | None = Field(default=None, ge=0, le=1000000)
    session_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9._:-]{1,80}$')
    sequence: int | None = Field(default=None, ge=0, le=1000000000)
    timestamp_ms: int | None = Field(default=None, ge=0, le=99999999999999)
    control: str | None = Field(default=None, max_length=300, pattern=r'^[a-z0-9:()>-]+$')


class ClientBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    events: list[ClientEvent] = Field(min_length=1, max_length=20)


@router.post('/client-events', status_code=204,
             dependencies=[Depends(RateLimitByIP(60, 'client-diagnostics'))])
async def client_events(request: Request, db=Depends(get_database)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16384:
            raise HTTPException(413, 'Diagnostics batch too large')
    try:
        batch = ClientBatch.model_validate(json.loads(data))
    except (ValueError, ValidationError):
        raise HTTPException(400, 'Invalid diagnostics batch')
    if request.cookies.get(settings.JWT_COOKIE_NAME):
        try:
            await get_current_identity(request, db)
        except HTTPException:
            pass  # Login failures must also be diagnosable; no client-supplied identity.
    for item in batch.events:
        fields = item.model_dump(exclude_none=True)
        name = fields.pop('event')
        # Query strings/fragments must not carry credentials or search/input text.
        for key in ('path','page'):
            fields[key] = fields[key].split('?',1)[0].split('#',1)[0]
        event('browser.'+name, source='browser_untrusted', client=fields)
    return Response(status_code=204)
