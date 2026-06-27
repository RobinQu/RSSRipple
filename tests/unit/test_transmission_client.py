"""Unit tests for TransmissionWrapper — no real Transmission daemon required."""

from unittest.mock import MagicMock, patch

import pytest

from app.clients.transmission import TransmissionWrapper, _parse_url


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

class TestParseUrl:
    def test_full_url(self):
        result = _parse_url("http://transmission:9091/transmission/rpc")
        assert result == {
            "protocol": "http",
            "host": "transmission",
            "port": 9091,
            "path": "/transmission/rpc",
        }

    def test_https(self):
        result = _parse_url("https://tr.example.com:443/rpc")
        assert result["protocol"] == "https"
        assert result["host"] == "tr.example.com"
        assert result["port"] == 443

    def test_localhost_default_port(self):
        result = _parse_url("http://localhost:9091/transmission/rpc")
        assert result["host"] == "localhost"
        assert result["port"] == 9091

    def test_custom_path(self):
        result = _parse_url("http://nas:9091/my/custom/rpc")
        assert result["path"] == "/my/custom/rpc"


# ---------------------------------------------------------------------------
# TransmissionWrapper — mocking transmission_rpc.Client
# ---------------------------------------------------------------------------

MOCK_CLIENT = "app.clients.transmission.TransmissionClient"


def _make_wrapper(url="http://localhost:9091/transmission/rpc",
                  username=None, password=None):
    return TransmissionWrapper(url, username, password)


class TestTestConnection:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_session.version = "4.0.5"
        mock_client.get_session.return_value = mock_session

        with patch(MOCK_CLIENT, return_value=mock_client):
            wrapper = _make_wrapper()
            ok, detail = await wrapper.test_connection()

        assert ok is True
        assert "4.0.5" in detail

    @pytest.mark.asyncio
    async def test_failure_connection_refused(self):
        mock_client = MagicMock()
        mock_client.get_session.side_effect = ConnectionRefusedError("Connection refused")

        with patch(MOCK_CLIENT, return_value=mock_client):
            wrapper = _make_wrapper()
            ok, detail = await wrapper.test_connection()

        assert ok is False
        assert detail is not None

    @pytest.mark.asyncio
    async def test_failure_auth_error(self):
        mock_client = MagicMock()
        mock_client.get_session.side_effect = Exception("403 Forbidden")

        with patch(MOCK_CLIENT, return_value=mock_client):
            wrapper = _make_wrapper(username="user", password="wrong")
            ok, detail = await wrapper.test_connection()

        assert ok is False
        assert "403" in detail

    @pytest.mark.asyncio
    async def test_credentials_passed_to_client(self):
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_session.version = "3.0"
        mock_client.get_session.return_value = mock_session

        with patch(MOCK_CLIENT, return_value=mock_client) as MockCls:
            wrapper = _make_wrapper(username="admin", password="secret")
            await wrapper.test_connection()
            _, kwargs = MockCls.call_args
            assert kwargs.get("username") == "admin"
            assert kwargs.get("password") == "secret"


class TestAddTorrent:
    @pytest.mark.asyncio
    async def test_add_returns_metadata(self):
        mock_torrent = MagicMock()
        mock_torrent.id = 42
        mock_torrent.name = "My.Show.S01E01"
        mock_torrent.hashString = "abc123"
        mock_client = MagicMock()
        mock_client.add_torrent.return_value = mock_torrent

        with patch(MOCK_CLIENT, return_value=mock_client):
            wrapper = _make_wrapper()
            result = await wrapper.add_torrent("https://example.com/file.torrent")

        assert result["torrent_id"] == 42
        assert result["name"] == "My.Show.S01E01"
        assert result["hash"] == "abc123"
        mock_client.add_torrent.assert_called_once_with(
            "https://example.com/file.torrent", paused=False
        )

    @pytest.mark.asyncio
    async def test_add_with_download_dir(self):
        mock_torrent = MagicMock(id=1, name="x", hashString="h")
        mock_client = MagicMock()
        mock_client.add_torrent.return_value = mock_torrent

        with patch(MOCK_CLIENT, return_value=mock_client):
            wrapper = _make_wrapper()
            await wrapper.add_torrent("magnet:?xt=...", download_dir="/tmp/dl")

        _, kwargs = mock_client.add_torrent.call_args
        assert kwargs.get("download_dir") == "/tmp/dl"


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_success(self):
        mock_client = MagicMock()
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().pause_torrent(1)
        assert ok is True
        mock_client.stop_torrent.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_pause_failure(self):
        mock_client = MagicMock()
        mock_client.stop_torrent.side_effect = Exception("not found")
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().pause_torrent(99)
        assert ok is False

    @pytest.mark.asyncio
    async def test_resume_success(self):
        mock_client = MagicMock()
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().resume_torrent(1)
        assert ok is True
        mock_client.start_torrent.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_resume_failure(self):
        mock_client = MagicMock()
        mock_client.start_torrent.side_effect = Exception("error")
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().resume_torrent(1)
        assert ok is False


class TestListTorrents:
    def _make_torrent(self, id=1, name="Show.S01E01", status="downloading",
                      percent_done=0.5, rate_dl=1024, rate_ul=512,
                      eta_seconds=300, total_size=1_000_000, have_valid=500_000,
                      is_finished=False, error=0, left_until_done=0):
        from unittest.mock import PropertyMock
        from datetime import timedelta, datetime, timezone
        t = MagicMock()
        t.id = id
        t.name = name
        t.hashString = "abc123"
        t.status = status
        t.percentDone = percent_done
        t.rateDownload = rate_dl
        t.rateUpload = rate_ul
        t.eta = timedelta(seconds=eta_seconds)
        t.totalSize = total_size
        t.haveValid = have_valid
        t.isFinished = is_finished
        t.error = error
        t.leftUntilDone = left_until_done
        t.addedDate = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t.fields = {"errorString": "", "peersConnected": 3}
        return t

    @pytest.mark.asyncio
    async def test_returns_list(self):
        mock_client = MagicMock()
        mock_client.get_torrents.return_value = [
            self._make_torrent(id=1, name="A"),
            self._make_torrent(id=2, name="B"),
        ]
        with patch(MOCK_CLIENT, return_value=mock_client):
            result = await _make_wrapper().list_torrents()
        assert len(result) == 2
        assert result[0]["name"] == "A"
        assert result[1]["name"] == "B"

    @pytest.mark.asyncio
    async def test_fields_normalized(self):
        mock_client = MagicMock()
        mock_client.get_torrents.return_value = [self._make_torrent(
            status="downloading", percent_done=0.42,
            rate_dl=1_048_576, rate_ul=10_240, eta_seconds=600,
        )]
        with patch(MOCK_CLIENT, return_value=mock_client):
            result = await _make_wrapper().list_torrents()
        t = result[0]
        assert t["status"] == "downloading"
        assert t["percent_done"] == pytest.approx(0.42)
        assert t["rate_download"] == 1_048_576
        assert t["rate_upload"] == 10_240
        assert t["eta_seconds"] == 600
        assert t["peers_connected"] == 3

    @pytest.mark.asyncio
    async def test_eta_none_when_negative(self):
        from datetime import timedelta
        mock_client = MagicMock()
        torrent = self._make_torrent()
        torrent.eta = timedelta(seconds=-1)
        mock_client.get_torrents.return_value = [torrent]
        with patch(MOCK_CLIENT, return_value=mock_client):
            result = await _make_wrapper().list_torrents()
        assert result[0]["eta_seconds"] is None

    @pytest.mark.asyncio
    async def test_eta_none_when_not_set(self):
        mock_client = MagicMock()
        torrent = self._make_torrent()
        torrent.eta = None
        mock_client.get_torrents.return_value = [torrent]
        with patch(MOCK_CLIENT, return_value=mock_client):
            result = await _make_wrapper().list_torrents()
        assert result[0]["eta_seconds"] is None

    @pytest.mark.asyncio
    async def test_requests_required_fields(self):
        mock_client = MagicMock()
        mock_client.get_torrents.return_value = []
        with patch(MOCK_CLIENT, return_value=mock_client):
            await _make_wrapper().list_torrents()
        _, kwargs = mock_client.get_torrents.call_args
        fields = kwargs.get("arguments", [])
        for required in ("id", "name", "status", "percentDone", "rateDownload", "rateUpload", "eta"):
            assert required in fields, f"'{required}' missing from requested fields"

    @pytest.mark.asyncio
    async def test_propagates_connection_error(self):
        mock_client = MagicMock()
        mock_client.get_torrents.side_effect = ConnectionRefusedError("refused")
        with patch(MOCK_CLIENT, return_value=mock_client):
            with pytest.raises(ConnectionRefusedError):
                await _make_wrapper().list_torrents()


class TestRemoveTorrent:
    @pytest.mark.asyncio
    async def test_remove_no_data(self):
        mock_client = MagicMock()
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().remove_torrent(5)
        assert ok is True
        mock_client.remove_torrent.assert_called_once_with(5, delete_data=False)

    @pytest.mark.asyncio
    async def test_remove_with_data(self):
        mock_client = MagicMock()
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().remove_torrent(5, delete_data=True)
        assert ok is True
        mock_client.remove_torrent.assert_called_once_with(5, delete_data=True)

    @pytest.mark.asyncio
    async def test_remove_failure(self):
        mock_client = MagicMock()
        mock_client.remove_torrent.side_effect = Exception("gone")
        with patch(MOCK_CLIENT, return_value=mock_client):
            ok = await _make_wrapper().remove_torrent(1)
        assert ok is False
