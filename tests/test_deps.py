import pytest
from unittest.mock import patch, MagicMock
from api.deps import _ensure_history_schema, save_query

@pytest.fixture(autouse=True)
def reset_history_schema_initialized():
    import api.deps
    api.deps._history_schema_initialized = False
    yield
    api.deps._history_schema_initialized = False

@patch('api.deps._pg_connect')
def test_ensure_history_schema_closes_connection(mock_pg_connect):
    mock_conn = MagicMock()
    mock_pg_connect.return_value = mock_conn
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    _ensure_history_schema()

    mock_cur.execute.assert_called_once()
    mock_conn.close.assert_called_once()

@patch('api.deps._pg_connect')
def test_save_query_closes_connection(mock_pg_connect):
    mock_conn = MagicMock()
    mock_pg_connect.return_value = mock_conn
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    save_query("testuser", "testquery", "testresponse")

    # Should be called twice: once for _ensure_history_schema, once for save_query
    assert mock_cur.execute.call_count == 2
    assert mock_conn.close.call_count == 2
