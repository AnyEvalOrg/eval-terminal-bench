"""Harbor 0.22 compatibility hook, active only for the AnyEval environment.

The stock verifier still stages and executes tests exactly once. Recovery resumes
at download/reward parsing, never at verify(). Separate verifier images own /tests.
"""
import asyncio
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
        sources, source_root, entrypoint = self._resolve_tests()
        previous = environment._verifier_guard
        environment._verifier_guard = {
            "pending_uploads": set(sources), "checked": False,
            "script": str(EnvironmentPaths.for_os(environment.os).tests_dir /
                          entrypoint.relative_to(source_root).as_posix()),
            "source": "prebuilt-image" if self._skip_tests_upload else "uploaded",
        }
        try:
            try:
                return await original(self)
            except DownloadVerifierDirError:
                return await recover_download(self)
        finally:
            environment._verifier_guard = previous

    verify._anyeval_hook = True
    Verifier.verify = verify
