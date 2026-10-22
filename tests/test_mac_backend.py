"""The Mac window should join the dashboard already shown by the CLI."""

from unittest.mock import MagicMock, patch

from app.backend import running_standard_dashboard


def test_running_standard_dashboard_is_reused() -> None:
    response = MagicMock(status=200)
    response.read.return_value = (
        b'{"ok":true,"service":"spiritboard","instance_id":"1234567890abcdef1234567890abcdef"}'
    )
    with patch("http.client.HTTPConnection") as connection:
        connection.return_value.getresponse.return_value = response
        assert running_standard_dashboard() == "http://127.0.0.1:8766"
        connection.return_value.request.assert_called_once_with("GET", "/api/health")


def test_other_local_services_are_ignored() -> None:
    response = MagicMock(status=200)
    response.read.return_value = b'{"ok":true,"service":"another"}'
    with patch("http.client.HTTPConnection") as connection:
        connection.return_value.getresponse.return_value = response
        assert running_standard_dashboard() is None
