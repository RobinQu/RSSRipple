import pytest

from app.services.organize_ownership import OwnershipDomainError, plan_lock, registered_domain


async def test_lock_domain_rejects_second_directory_and_missing_marker(db_session, tmp_path):
    first, second = tmp_path / "shared", tmp_path / "private-worker"
    domain = await registered_domain(db_session, first)
    assert await registered_domain(db_session, first) == domain
    second.mkdir()
    with pytest.raises(OwnershipDomainError, match="缺少已注册身份"):
        await registered_domain(db_session, second)
    assert not (second / ".domain").exists()
    (first / ".domain").unlink()
    with pytest.raises(OwnershipDomainError, match="缺少已注册身份"):
        await registered_domain(db_session, first)
    assert not (first / ".domain").exists()


async def test_lock_domain_rechecked_on_open_directory(db_session, tmp_path):
    import uuid

    domain = await registered_domain(db_session, tmp_path)
    (tmp_path / ".domain").write_text(str(uuid.uuid4()))
    with pytest.raises(OwnershipDomainError, match="身份.*变化"):
        with plan_lock(tmp_path, "test-plan", domain):
            pytest.fail("mismatched directory acquired")


async def test_lock_domain_rejects_symlink(db_session, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OwnershipDomainError, match="目录不可用"):
        await registered_domain(db_session, link)
    assert list(real.iterdir()) == []
