"""The retired Git mailbox repository cache must not regain a public surface."""

import pytest

from mcp_agent_mail import storage


@pytest.mark.parametrize(
    "name",
    (
        "clear_repo_cache",
        "close_repo",
        "get_cached_repo",
        "proactive_fd_cleanup",
        "ensure_archive_root",
        "ensure_archive",
        "ProjectArchive",
        "create_diagnostic_backup",
        "list_backups",
        "restore_from_backup",
        "heal_archive_locks",
        "get_recent_commits",
        "get_commit_detail",
        "get_message_commit_sha",
        "get_archive_tree",
        "get_file_content",
        "get_agent_communication_graph",
        "get_timeline_commits",
        "get_historical_inbox_snapshot",
    ),
)
def test_git_mailbox_repository_cache_api_is_retired(name):
    assert not hasattr(storage, name)
