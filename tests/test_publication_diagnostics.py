import json
import pytest
from scripts.publish_specialist_review import _report_publication


def test_publication_error_logged_and_saved_in_uploaded_artifact(tmp_path, capsys):
    artifact = tmp_path / 'artifact.json'
    artifact.write_text(json.dumps({'publishing': {'ready': True}}))
    state = {'review_completed': False, 'publication_errors': [
        {'operation': 'query_managed_state', 'error': 'managed review object is invalid'}]}
    _report_publication(state, str(artifact))
    assert 'query_managed_state' in capsys.readouterr().err
    saved = json.loads(artifact.read_text())
    assert saved['publishing']['status'] == 'failed'
    assert saved['publishing']['publication_state'] == state


def test_publication_logging_masks_credentials(capsys):
    _report_publication({'publication_errors': [{'operation': 'submit',
                         'error': 'password=super-secret-value'}]}, None)
    assert 'super-secret-value' not in capsys.readouterr().err


def test_failed_artifact_update_preserves_original(tmp_path, monkeypatch):
    artifact = tmp_path / 'artifact.json'
    artifact.write_text('{"publishing": {"ready": true}}')
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr('scripts.publish_specialist_review.os.replace', fail)
    with pytest.raises(OSError, match='replace failed'):
        _report_publication({'review_completed': False, 'publication_errors': []}, str(artifact))
    assert artifact.read_text() == '{"publishing": {"ready": true}}'
    assert list(tmp_path.iterdir()) == [artifact]
