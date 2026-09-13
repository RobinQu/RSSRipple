"""Media-server protocol fault injection through the scan API and real DB.

Locations here are synthetic protocol inputs, not captured production data.
An existing library and a valid preceding row detect partial mutation.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.v1.media_servers import router
from app.database import get_db
from app.models.library import Library
from app.models.media_server import MediaServerBinding, MediaServerInstance


@pytest.mark.parametrize('scenario', ['valid', 'parent', 'absolute_suffix', 'drive', 'control', 'symlink'])
async def test_scan_rejects_entire_unsafe_batch(
    db_session, session_factory, shared_volume, monkeypatch, scenario,
):
    root = shared_volume.process
    outside = root.parent / 'outside-library'
    outside.mkdir()
    sentinel = outside / 'sentinel.mkv'
    sentinel.write_bytes(b'not library content')
    (root / 'linked').symlink_to(outside, target_is_directory=True)
    server = MediaServerInstance(
        id=str(uuid.uuid4()), name='boundary-server', type='plex',
        url='http://media.invalid', token='test-only',
    )
    server.bindings.append(MediaServerBinding(
        server_path_prefix='/media', volume_id=shared_volume.volume.id, subpath='',
    ))
    db_session.add(server)
    await db_session.commit()
    existing = Library(
        id=str(uuid.uuid4()), name='original name', kind='tv',
        media_server_id=server.id, section_key='existing', server_path='/media/good',
        volume_id=shared_volume.volume.id, root_subpath='good',
    )
    db_session.add(existing)
    await db_session.commit()
    locations = {
        'valid': '/media/nested/tv', 'parent': '/media/../outside-library',
        'absolute_suffix': '/media//outside-library', 'drive': '/media/C:outside',
        'control': '/media/bad\x00name', 'symlink': '/media/linked',
    }
    sections = [
        {'key': 'existing', 'name': 'changed name', 'kind': 'tv', 'paths': ['/media/good']},
        {'key': 'new', 'name': 'new library', 'kind': 'tv', 'paths': ['/media/new']},
        {'key': 'last', 'name': 'last library', 'kind': 'tv', 'paths': [locations[scenario]]},
    ]
    client = SimpleNamespace(list_libraries=AsyncMock(return_value=sections))
    monkeypatch.setattr('app.services.media_server_service.get_client', lambda _: client)
    api = FastAPI()
    api.include_router(router, prefix='/api/v1')

    async def test_db():
        yield db_session

    api.dependency_overrides[get_db] = test_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as http:
        for _ in range(2):
            response = await http.post(f'/api/v1/media-servers/{server.id}/scan')
            if scenario == 'valid':
                assert response.status_code == 200, response.text
            else:
                assert response.status_code == 502, response.text
                assert response.json()['error']['code'] == 'MEDIA_SERVER_ERROR'
                assert not db_session.new and not db_session.dirty
            async with session_factory() as session:
                rows = (await session.execute(select(Library))).scalars().all()
                if scenario == 'valid':
                    assert len(rows) == 3
                    assert {r.root_subpath for r in rows} == {'good', 'new', 'nested/tv'}
                else:
                    [row] = rows
                    assert row.id == existing.id
                    assert row.name == 'original name'
                    assert row.root_subpath == 'good'
            assert sentinel.read_bytes() == b'not library content'


@pytest.mark.parametrize("field", ["root_subpath", "recycle_subpath", "mount_path"])
async def test_library_api_exposes_invalid_legacy_path(db_session, shared_volume, field):
    from app.api.v1.organize import router as organize_router

    library = Library(id=str(uuid.uuid4()), name="legacy", kind="tv",
                      volume_id=shared_volume.volume.id)
    if field == "mount_path":
        shared_volume.volume.mount_path = "/bad\x00mount"
    else:
        setattr(library, field, "../outside-library")
    db_session.add(library)
    await db_session.commit()
    api = FastAPI()
    api.include_router(organize_router, prefix="/api/v1")

    async def test_db():
        yield db_session

    api.dependency_overrides[get_db] = test_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as http:
        response = await http.get(f"/api/v1/libraries/{library.id}")
        assert response.status_code == 200, response.text
        value = response.json()["data"]
        assert value["path_error"] and "路径" in value["path_error"]
        assert value["root_path"] is None
        assert value["bound"]  # binding exists; resolution is independently invalid
        response = await http.get("/api/v1/libraries")
        assert response.status_code == 200, response.text
        assert response.json()["data"][0]["path_error"]
