"""Git mailbox commit scheduling is retired with the Git data plane."""

from mcp_agent_mail import storage


def test_git_mailbox_commit_queue_api_is_retired():
    assert not hasattr(storage, "_CommitQueue")
    assert not hasattr(storage, "_get_commit_queue")
    assert not hasattr(storage, "_commit")
    assert not hasattr(storage, "_commit_direct")
    assert not hasattr(storage, "_commit_lock_path")
    assert not hasattr(storage, "GitIndexLockError")
