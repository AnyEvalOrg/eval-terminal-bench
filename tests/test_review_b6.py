import asyncio
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from harbor.utils.scripts import build_execution_command
from terminal_bench_anyeval import bounded_io
from terminal_bench_anyeval.k8s_env import AnyEvalK8sEnvironment, VerifierPreflightError
from terminal_bench_anyeval.trial import classify


@pytest.fixture
def env():
    environment = object.__new__(AnyEvalK8sEnvironment)
    environment.task_env_config = SimpleNamespace(workdir=None)
    environment.max_transfer_bytes = 100_000
    environment.max_archive_members = 100
    environment.transfer_timeout_sec = 10
    environment._verifier_guard = None
    environment._merge_env = MagicMock(return_value={})
    environment._resolve_user = MagicMock(return_value=None)
    environment._output_callback = MagicMock(return_value=None)
    environment._save_facts = MagicMock()
    environment._require_running = AsyncMock()
    environment._stream = AsyncMock(return_value=(b"", b"", 0))
    return environment


def guard():
    return {
        "script": "/tests/test.sh",
        "command": build_execution_command(
            "/tests/test.sh", stdout_path="/logs/verifier/test-stdout.txt", task_os="linux"
        ),
        "pending_uploads": set(),
        "checked": False,
        "execution_completed": False,
        "source": "uploaded",
    }


def test_b6_preflight_follows_chmod_and_only_checks_verifier(env, tmp_path):
    entrypoint = tmp_path / "entrypoint"
    entrypoint.write_bytes(b"exit 0\n")
    entrypoint.chmod(0o644)
    env._verifier_guard = guard()
    events = []

    async def remote_exec(argv, **kwargs):
        if "chmod +x" in argv[-1]:
            entrypoint.chmod(0o755)
            events.append("chmod")
        else:
            events.append("exec")
        return b"", b"", 0

    async def preflight(argv, **kwargs):
        events.append("preflight")
        return b"", b"", 0 if entrypoint.stat().st_mode & 0o111 else 1

    env._stream.side_effect = remote_exec
    env._transfer_stream = AsyncMock(side_effect=preflight)
    env._check_verifier_guard = AsyncMock(wraps=env._check_verifier_guard)

    async def run():
        await env.exec("mkdir -p /logs/verifier")
        await env.exec("chmod +x /tests/test.sh")
        assert not env._verifier_guard["checked"]
        env._check_verifier_guard.assert_not_awaited()
        await env.exec(env._verifier_guard["command"])
        await env.exec("true")

    asyncio.run(run())
    assert events == ["exec", "chmod", "preflight", "exec", "exec"]
    env._check_verifier_guard.assert_awaited_once()
    assert env._verifier_guard["checked"]
    assert env._verifier_guard["execution_completed"]


@pytest.mark.parametrize("failure", ["pending_upload", "not_running", "not_executable"])
def test_b6_verifier_preflight_still_refuses_unready_verifier(env, failure):
    env._verifier_guard = guard()
    env._transfer_stream = AsyncMock(return_value=(b"", b"", 0))
    if failure == "pending_upload":
        env._verifier_guard["pending_uploads"].add(Path("pending"))
    elif failure == "not_running":
        env._require_running.side_effect = VerifierPreflightError("Pod is not Running")
    else:
        env._transfer_stream.return_value = b"", b"", 1
    with pytest.raises(VerifierPreflightError):
        asyncio.run(env.exec(env._verifier_guard["command"]))
    env._stream.assert_not_awaited()
    assert not env._verifier_guard["checked"]
    assert not env._verifier_guard["execution_completed"]


def test_b6_pack_normalizes_all_member_ownership(env, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    item = source / "item"
    item.write_bytes(b"synthetic upload\n")
    item.chmod(0o644)
    os.link(item, source / "hardlink")
    (source / "symlink").symlink_to("item")
    original = tarfile.TarFile.gettarinfo

    def host_owned(archive, *args, **kwargs):
        member = original(archive, *args, **kwargs)
        member.uid, member.gid = 501, 20
        member.uname, member.gname = "host-user", "host-group"
        return member

    monkeypatch.setattr(tarfile.TarFile, "gettarinfo", host_owned)
    data = io.BytesIO()
    env._pack(source, ".", data)
    with tarfile.open(fileobj=data, mode="r:") as archive:
        entries = archive.getmembers()
        assert {m.type for m in entries} == {
            tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.LNKTYPE, tarfile.SYMTYPE
        }
        assert all((m.uid, m.gid, m.uname, m.gname) == (0, 0, "root", "root") for m in entries)
        assert all(m.mode == 0o644 for m in entries if m.isfile())
    assert item.stat().st_mode & 0o777 == 0o644


def test_b6_upload_extracts_with_no_same_owner(env, tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo("payload")
        member.uid, member.gid = 501, 20
        member.uname, member.gname = "host-user", "host-group"
        member.size = 7
        archive.addfile(member, io.BytesIO(b"payload"))
    data.seek(0)
    target = tmp_path / "upload with spaces"

    async def extract(argv, *, data, **kwargs):
        tokens = shlex.split(argv[-1])
        tar_args = tokens[tokens.index("tar") + 1:]
        assert "--no-same-owner" in tar_args
        result = subprocess.run(argv, input=data.read(), capture_output=True)
        return result.stdout, result.stderr, result.returncode

    env._transfer_stream = AsyncMock(side_effect=extract)
    asyncio.run(env._upload(data, str(target)))
    assert (target / "payload").read_bytes() == b"payload"
    assert (target / "payload").stat().st_uid == os.getuid()


def hardlink_archive(size=65_536, links=20, chained=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo("original")
        member.size = size
        archive.addfile(member, io.BytesIO(b"x" * size))
        previous = member.name
        for index in range(links):
            member = tarfile.TarInfo(f"copy-{index:02d}")
            member.type = tarfile.LNKTYPE
            member.linkname = previous if chained else "original"
            archive.addfile(member)
            previous = member.name
    return data.getvalue()


@pytest.mark.parametrize("mode", ["filtered", "filtered_subset", "unfiltered"])
def test_b6_hardlink_download_budget_refuses_before_writing(env, tmp_path, mode):
    payload = hardlink_archive()
    assert len(payload) == 81_920 < env.max_transfer_bytes
    env._download = AsyncMock(return_value=payload)
    target = tmp_path / "download"
    if mode == "unfiltered":
        operation = env.download_dir("/logs/verifier", target)
    else:
        operation = env.download_dir_filtered(
            source_dir="/logs/verifier", target_dir=target,
            include=["copy-19"] if mode == "filtered_subset" else None,
        )
    with pytest.raises(bounded_io.TransferLimitError) as error:
        asyncio.run(operation)
    assert classify(type(error.value).__name__) == "infrastructure"
    assert not any(p.is_file() for p in target.rglob("*"))


@pytest.mark.parametrize("chained", [False, True])
def test_b6_hardlink_budget_exact_boundary_and_chain(chained):
    payload = hardlink_archive(size=32_768, links=2, chained=chained)
    entries = bounded_io.inventory(payload, 98_304, 100)
    assert len(entries) == 3
    assert len(set(entries.values())) == 1
    with pytest.raises(bounded_io.TransferLimitError):
        bounded_io.inventory(payload, 98_303, 100)


def test_b6_members_charges_hardlinks_before_yielding():
    written = 0
    with tarfile.open(fileobj=io.BytesIO(hardlink_archive()), mode="r:") as archive:
        with pytest.raises(bounded_io.TransferLimitError):
            for member in bounded_io.members(archive, 100_000, 100):
                with archive.extractfile(member) as stream:
                    written += len(stream.read())
                assert written <= 100_000
    assert written == 65_536
