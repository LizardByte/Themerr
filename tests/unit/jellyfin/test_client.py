"""Jellyfin failure diagnostics retain request context without upstream secrets."""

# standard imports
from unittest.mock import Mock

# lib imports
import pytest
import requests

# local imports
from jellyfin import backend, connector, servers
from jellyfin.client import Client
from media_servers.base import MediaServerError
from themerr import storage


ROUTE = f'/Themerr/Items/{"1" * 32}/Theme'
SECRET = 'private-api-key'


@pytest.mark.parametrize('status', [
    302,
    400,
    413,
    415,
    500,
    502,
    503,
])
def test_http_failure_reports_status_and_logs_route_without_upstream_details(monkeypatch, caplog, status):
    result = Mock(status_code=status, text=f'private server error: {SECRET}', reason=SECRET)
    monkeypatch.setattr(requests, 'request', Mock(return_value=result))

    with pytest.raises(MediaServerError) as error:
        Client('http://jellyfin.example', SECRET).request('POST', ROUTE)

    assert error.value.status_code == 502
    assert f'HTTP {status}' in str(error.value)
    assert 'Jellyfin server log' in str(error.value)
    assert f'POST {ROUTE}' in caplog.text
    assert f'HTTP {status}' in caplog.text
    assert SECRET not in f'{error.value}{caplog.text}'
    assert 'private server error' not in f'{error.value}{caplog.text}'
    result.close.assert_called_once()


@pytest.mark.parametrize('status, message', [
    (
        401,
        'Jellyfin rejected the API key.',
    ),
    (
        403,
        'An administrator Jellyfin API key is required.',
    ),
    (
        404,
        'This Jellyfin resource is unavailable.',
    ),
    (
        409,
        'Jellyfin protected an existing theme or rejected the connector version.',
    ),
])
def test_known_http_errors_keep_their_public_status_and_message(monkeypatch, status, message):
    result = Mock(status_code=status)
    monkeypatch.setattr(requests, 'request', Mock(return_value=result))

    with pytest.raises(MediaServerError, match=message) as error:
        Client('http://jellyfin.example', SECRET).request('POST', ROUTE)

    assert error.value.status_code == status
    result.close.assert_called_once()


@pytest.mark.parametrize('failure', [
    requests.ConnectionError,
    requests.Timeout,
])
def test_transport_failure_logs_its_type_without_exception_details(monkeypatch, caplog, failure):
    monkeypatch.setattr(requests, 'request', Mock(side_effect=failure(f'private connection error: {SECRET}')))

    with pytest.raises(MediaServerError, match='Could not reach Jellyfin') as error:
        Client('http://jellyfin.example', SECRET).request('POST', ROUTE)

    assert error.value.status_code == 502
    assert f'POST {ROUTE}' in caplog.text
    assert failure.__name__ in caplog.text
    assert SECRET not in f'{error.value}{caplog.text}'


def test_invalid_json_logs_the_request_and_closes_the_response(monkeypatch, caplog):
    result = Mock(status_code=200, json=Mock(side_effect=ValueError(f'private response: {SECRET}')))
    monkeypatch.setattr(requests, 'request', Mock(return_value=result))

    with pytest.raises(MediaServerError, match='Jellyfin returned an invalid response'):
        Client('http://jellyfin.example', SECRET).json('POST', ROUTE)

    assert f'POST {ROUTE}' in caplog.text
    assert 'invalid JSON' in caplog.text
    assert SECRET not in caplog.text
    result.close.assert_called_once()


def test_success_does_not_log_a_failure_or_close_a_stream(monkeypatch, caplog):
    result = Mock(status_code=200)
    monkeypatch.setattr(requests, 'request', Mock(return_value=result))

    assert Client('http://jellyfin.example', SECRET).request('GET', ROUTE, stream=True) is result

    assert not caplog.text
    result.close.assert_not_called()


@pytest.mark.parametrize('status, expected_reason', [
    (
        400,
        'Put each movie in its own folder',
    ),
    (
        500,
        'HTTP 500',
    ),
])
def test_failed_theme_request_reaches_dashboard_and_logs_with_server_context(
        configured, connector_bundle, monkeypatch, caplog, status, expected_reason):
    item_id = '1' * 32
    server_id = f'jellyfin:{"3" * 32}'
    items = Mock(status_code=200, json=Mock(return_value={
        'Items': [{
            'Id': item_id,
            'Name': 'Example',
            'Type': 'Movie',
        }],
        'TotalRecordCount': 1,
    }))
    failure = Mock(status_code=status, text=SECRET)
    request = Mock(side_effect=[
        items,
        failure,
    ])
    monkeypatch.setattr(requests, 'request', request)
    monkeypatch.setattr(servers, 'client', lambda _: Client('http://jellyfin.example', SECRET))
    monkeypatch.setattr(servers, 'get_server', lambda _: {'ignored_libraries': ''})
    monkeypatch.setattr(connector, 'verify', lambda _: connector_bundle)

    assert backend.JellyfinMediaServer(server_id).update_item(item_id) is False

    with storage.server_scope(server_id):
        reason = storage.get_errors()[item_id]
        assert expected_reason in reason
        assert storage.get_tracking(item_id) == {}
    assert f'GET {ROUTE}' in caplog.text
    assert server_id in caplog.text
    assert f'HTTP {status}' in caplog.text
    assert SECRET not in f'{reason}{caplog.text}'
    assert all('error_messages' not in call.kwargs for call in request.call_args_list)
    items.close.assert_called_once()
    failure.close.assert_called_once()
