import json

import pytest

from episode.config import EpisodeConfig, load_config


def test_onvif_snapshot_action_is_disabled_by_default():
    assert EpisodeConfig().actions.snapshot.enabled is False


def test_onvif_snapshot_action_can_be_enabled_explicitly():
    config = EpisodeConfig(actions={"snapshot": {"enabled": True}})

    assert config.actions.snapshot.enabled is True


def test_recording_fragments_default_to_four_seconds():
    assert EpisodeConfig().actions.recording.fragment_seconds == 4


def test_recording_fragment_duration_can_be_configured():
    config = EpisodeConfig(actions={"recording": {"fragment_seconds": 6}})

    assert config.actions.recording.fragment_seconds == 6


def test_recording_fragment_duration_must_be_bounded():
    with pytest.raises(ValueError, match="fragment_seconds must be between 1 and 30"):
        EpisodeConfig(actions={"recording": {"fragment_seconds": 0}})


def test_obsolete_inventory_configuration_is_rejected(tmp_path):
    path = tmp_path / "episode.json"
    path.write_text(json.dumps({"areas": []}))

    with pytest.raises(ValueError, match="unexpected keyword argument 'areas'"):
        load_config(str(path))
