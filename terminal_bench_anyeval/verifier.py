"""Harbor 0.22 compatibility hook, active only for the AnyEval environment.

The stock verifier still stages and executes tests exactly once. Recovery resumes
at download/reward parsing, never at verify(). Separate verifier images own /tests.
"""
import asyncio
from .verifier_health import _SETUP_FAILURE
from functools import wraps

from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import DownloadVerifierDirError, RewardFileNotFoundError, Verifier


async def recover_download(verifier):
    environment = verifier.environment
    await asyncio.sleep(1)
    await environment._require_running()
    source = str(EnvironmentPaths.for_os(environment.os).verifier_dir)
    try:
        if verifier.include_logs or verifier.exclude_logs:
            await environment.download_dir_filtered(
                source_dir=source, target_dir=verifier.trial_paths.verifier_dir,
                include=verifier.include_logs or None, exclude=verifier.exclude_logs or None,
                protect=[verifier.trial_paths.reward_text_path.name,
                         verifier.trial_paths.reward_json_path.name])
        else:
            await environment.download_dir(source, verifier.trial_paths.verifier_dir)
    except Exception as exc:
        raise DownloadVerifierDirError("Verifier directory download failed after one recovery attempt") from exc
    environment._save_facts({"verifier_download_recovered": True})
    if verifier.trial_paths.reward_json_path.exists():
        rewards = verifier._parse_reward_json()
    elif verifier.trial_paths.reward_text_path.exists():
        rewards = verifier._parse_reward_text()
    else:
        raise RewardFileNotFoundError("No reward file after verifier directory recovery")
    return VerifierResult(rewards=rewards)


def install_verifier_hook(environment_class):
    if getattr(Verifier.verify, "_anyeval_hook", False):
        return
    original = Verifier.verify

    @wraps(original)
    async def verify(self):
        environment = self.environment
        if not isinstance(environment, environment_class):
            return await original(self)
        await environment._require_running()
        environment._save_facts({"verifier_health": {"setup_completed": False, "completed": False}})
        sources, source_root, entrypoint = self._resolve_tests()
        from harbor.utils.scripts import build_execution_command
        paths = EnvironmentPaths.for_os(environment.os)
        script = str(paths.tests_dir / entrypoint.relative_to(source_root).as_posix())
        stdout = str(paths.verifier_dir / self.trial_paths.test_stdout_path.relative_to(self.trial_paths.verifier_dir).as_posix())
        previous = environment._verifier_guard
        environment._verifier_guard = {
            "pending_uploads": set(sources), "checked": False, "execution_completed": False,
            "command": build_execution_command(script, stdout_path=stdout, task_os=environment.os),
            "script": str(EnvironmentPaths.for_os(environment.os).tests_dir /
                          entrypoint.relative_to(source_root).as_posix()),
            "source": "prebuilt-image" if self._skip_tests_upload else "uploaded",
        }
        try:
            try:
                result = await original(self)
            except DownloadVerifierDirError:
                result = await recover_download(self)
            guard = environment._verifier_guard
            text = self.trial_paths.test_stdout_path.read_text(errors="replace")
            unhealthy = _SETUP_FAILURE.search(text)
            environment._save_facts({"verifier_health": {
                "setup_completed": guard["checked"] and not bool(unhealthy),
                "completed": guard["execution_completed"] and not bool(unhealthy)}})
            if not (guard["checked"] and guard["execution_completed"] and not unhealthy):
                from .k8s_env import AnyEvalInfrastructureError
                raise AnyEvalInfrastructureError("Verifier health was not proved; reward refused")
            return result
        finally:
            environment._verifier_guard = previous

    verify._anyeval_hook = True
    Verifier.verify = verify


def install_artifact_hook(environment_class):
    """Scope read-back hashing to Harbor's actual agent-to-verifier transfers."""
    from harbor.trial.artifact_handler import ArtifactHandler
    original = ArtifactHandler.upload_artifacts
    if getattr(original, "_anyeval_hook", False):
        return

    @wraps(original)
    async def upload(self, target_env, *args, **kwargs):
        if not isinstance(target_env, environment_class):
            return await original(self, target_env, *args, **kwargs)
        previous = target_env._artifact_transfer
        target_env._artifact_transfer = True
        try:
            return await original(self, target_env, *args, **kwargs)
        finally:
            target_env._artifact_transfer = previous

    upload._anyeval_hook = True
    ArtifactHandler.upload_artifacts = upload
