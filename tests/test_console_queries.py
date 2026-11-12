"""Shared dashboard/agent query contracts and ownership regression coverage."""
import base64
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from max_agent.api.app import create_app
from max_agent.cli.main import app
from max_agent.db.models import TaskRow, TaskEventRow
from max_agent.db.session import session_scope
from max_agent.kernel.query import TaskQuery
from max_agent.kernel.service import TaskService
from max_agent.mcp.server import handle_message
from max_agent.models.schemas import TaskCreate

AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture()
def client(db_url, monkeypatch):
    monkeypatch.setenv('MAX_AGENT_API_ACTOR', 'alice')
    with TestClient(create_app(), headers=AUTH) as client:
        yield client


def seed(owner='alice', goal='a task', issue=None):
    with session_scope() as session:
        return TaskService(session).create(TaskCreate(goal=goal, issue_ref=issue), created_by=owner).id


def call(name, **arguments):
    result = handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': {'name': name, 'arguments': arguments}})['result']
    return result, json.loads(result['content'][0]['text'])


def test_search_literal_owner_order_and_cursor(client):
    ids = [seed(goal='find 100%_\\ literal', issue='org/repo#12') for _ in range(5)]
    seed(owner='bob', goal='find 100%_\\ literal', issue='org/repo#12')
    seed(goal='find 100XY literal')
    with session_scope() as session:
        session.query(TaskRow).filter(TaskRow.id.in_(ids)).update({'created_at': datetime(2026, 1, 1)})
    seen = []
    params = {'q': '100%_\\', 'limit': 2}
    while True:
        response = client.get('/v1/tasks/query', params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['total'] == 5
        seen += [row['id'] for row in page['items']]
        if not page['next_cursor']:
            break
        params['cursor'] = page['next_cursor']
    assert seen == sorted(ids, reverse=True)
    assert client.get('/v1/tasks/query', params={'q': ids[0]}).json()['total'] == 1
    assert client.get('/v1/tasks/query', params={'q': 'repo#12'}).json()['total'] == 5
    assert client.get('/v1/tasks/query', params={'status': 'succeeded'}).json()['total'] == 0
    assert client.get('/v1/tasks/query', params={**params, 'q': 'changed'}).status_code == 422
    assert isinstance(client.get('/v1/tasks').json(), list)
    with session_scope() as session:
        assert all(row.attempts == 0 for row in session.query(TaskRow))


@pytest.mark.parametrize('params', [
    {'limit': 0}, {'limit': 101}, {'limit': 'abc'}, {'q': 'x' * 257},
    {'status': 'nope'}, {'cursor': ''}, {'cursor': '!notbase64'},
    {'cursor': base64.urlsafe_b64encode(b'null').decode()}, {'cursor': 'x' * 1025},
])
def test_invalid_task_query(client, params):
    assert client.get('/v1/tasks/query', params=params).status_code == 422


def test_events_latest_window_and_older_pages(client):
    task_id = seed()
    with session_scope() as session:
        for i in range(211):
            session.add(TaskEventRow(task_id=task_id, event_type='verification', message=str(i), payload={'n': i}))
    url = f'/v1/tasks/{task_id}/events/query'
    page = client.get(url).json()
    assert len(page['items']) == 50
    assert page['items'][-1]['payload'] == {'n': 210}
    ids = [event['id'] for event in page['items']]
    assert ids == sorted(ids)
    while page['has_more']:
        page = client.get(url, params={'cursor': page['next_cursor']}).json()
        older = [event['id'] for event in page['items']]
        assert max(older) < min(ids)
        ids = older + ids
    assert len(ids) == len(set(ids)) == 213
    assert page['next_cursor'] is None
    assert len(client.get(f'/v1/tasks/{task_id}/events').json()) == 200
    for params in ({'limit': 201}, {'limit': 0}, {'cursor': 'bad'}):
        assert client.get(url, params=params).status_code == 422
    assert client.get(f'/v1/tasks/{task_id}/status').json()['verification'] == {'n': 210}
    result, body = call('max_status', task_id=task_id)
    assert not result['isError'] and body['verification'] == {'n': 210}
    cli = CliRunner().invoke(app, ['--json', 'status', task_id])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)['verification'] == {'n': 210}


def test_owner_auth_no_leaks_and_mcp_identity(client):
    alice = seed()
    bob = seed(owner='bob')
    for path in ('/v1/tasks/query', f'/v1/tasks/{alice}/events/query'):
        assert client.get(path, headers={'Authorization': ''}).status_code == 401
    assert client.get(f'/v1/tasks/{bob}/events/query').status_code == 403
    assert client.get('/v1/tasks/missing/events/query').status_code == 404
    _, page = call('max_list_tasks')
    assert [item['id'] for item in page['items']] == [alice]
    for name in ('max_status', 'max_events', 'max_cancel', 'max_approve', 'max_reject', 'max_trace'):
        result, body = call(name, task_id=bob)
        assert result['isError']
        assert body['task'] is None and body['events'] is None
    for name in ('max_approve', 'max_reject'):
        assert call(name, task_id=alice, actor='bob')[0]['isError']
    result, body = call('max_create', goal='MCP owned')
    assert not result['isError'] and body['task']['created_by'] == 'alice'
    assert client.get('/v1/tasks/query').json()['total'] == 2
    assert client.get('/v1/metrics').json()['verified_task_rate']['eligible'] == 2
    assert not call('max_trace', task_id=alice)[0]['isError']
    assert not call('max_approve', task_id=alice)[0]['isError']
    assert client.get(f'/v1/tasks/{alice}').json()['approved_by'] == 'alice'


def test_cursor_binding_and_runtime_validation(client):
    a, b = seed(), seed()
    page = client.get(f'/v1/tasks/{a}/events/query', params={'limit': 1}).json()
    assert client.get(f'/v1/tasks/{b}/events/query', params={'cursor': page['next_cursor']}).status_code == 422
    for arguments in ({'limit': True}, {'limit': '2'}, {'q': None}, {'cursor': 5}):
        assert call('max_list_tasks', **arguments)[0]['isError']
    with session_scope() as session:
        with pytest.raises(PermissionError):
            TaskQuery(session, 'bob').events(a)


def test_latest_verification_independent_of_recent_page(client):
    task_id = seed()
    with session_scope() as session:
        session.add(TaskEventRow(task_id=task_id, event_type='verification', message='evidence', payload={'passed': True}))
        session.flush()
        for _ in range(220):
            session.add(TaskEventRow(task_id=task_id, event_type='created', message='later journal entry', payload={}))
    recent = client.get(f'/v1/tasks/{task_id}/events/query').json()['items']
    assert all(event['event_type'] != 'verification' for event in recent)
    assert client.get(f'/v1/tasks/{task_id}/status').json()['verification'] == {'passed': True}
    assert call('max_status', task_id=task_id)[1]['verification'] == {'passed': True}
    assert call('max_events', task_id=task_id, limit=1)[1]['verification'] == {'passed': True}


def test_task_cursor_does_not_repeat_on_new_insert(client):
    original = [seed(goal='original') for _ in range(4)]
    page = client.get('/v1/tasks/query', params={'limit': 2}).json()
    first = [row['id'] for row in page['items']]
    new = seed(goal='new arrival')
    second = client.get('/v1/tasks/query', params={'limit': 2, 'cursor': page['next_cursor']}).json()
    remaining = [row['id'] for row in second['items']]
    assert len(set(first + remaining)) == 4
    assert set(first + remaining) == set(original)
    assert new not in remaining and second['total'] == 5


def test_structurally_invalid_scoped_cursor(client):
    seed()
    seed()
    cursor = client.get('/v1/tasks/query', params={'limit': 1}).json()['next_cursor']
    data = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    for position in ([None, 'x'], ['bad-date', 'x'], [], [datetime.now().isoformat(), {}]):
        data[2] = position
        malformed = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        assert client.get('/v1/tasks/query', params={'cursor': malformed}).status_code == 422


def test_mcp_shared_journal_page_and_foreign_cursor(client, monkeypatch):
    alice = seed()
    bob = seed(owner='bob')
    _, page = call('max_events_query', task_id=alice, limit=1)
    assert page == client.get(f'/v1/tasks/{alice}/events/query', params={'limit': 1}).json()
    _, older = call('max_events_query', task_id=alice, cursor=page['next_cursor'])
    assert older['items'][-1]['id'] < page['items'][0]['id']
    assert call('max_events_query', task_id=bob)[0]['isError']
    assert call('max_events_query', task_id=alice, limit=201)[0]['isError']
    # Generate a non-null cursor in Alice's scope, then switch configured owner.
    seed()
    task_cursor = client.get('/v1/tasks/query', params={'limit': 1}).json()['next_cursor']
    monkeypatch.setenv('MAX_AGENT_API_ACTOR', 'bob')
    assert call('max_list_tasks', cursor=task_cursor)[0]['isError']
    assert call('max_events_query', task_id=bob, cursor=page['next_cursor'])[0]['isError']
