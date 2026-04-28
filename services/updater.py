import asyncio
import logging
import os
import subprocess
from typing import Callable, Coroutine

logger = logging.getLogger("Updater")

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_EXAMPLE_PATH = os.path.join(REPO_DIR, ".env.example")
ENV_PATH = os.path.join(REPO_DIR, ".env")


def _parse_env_keys(filepath: str) -> set[str]:
    """Returns the set of variable names defined in an env file."""
    keys = set()
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.add(line.split("=", 1)[0].strip())
    except FileNotFoundError:
        pass
    return keys


GIT_ENV = {**os.environ, "LC_ALL": "C"}


async def check_for_updates() -> bool | None:
    """Checks if the local repository is behind the remote.
    Returns:
        True: Update available
        False: No update available
        None: Error during check (outage, timeout, etc.)
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "fetch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
            cwd=REPO_DIR
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            logger.error("Git fetch timed out")
            return None

        if process.returncode != 0:
            logger.error(f"Git fetch failed: {stderr.decode().strip()}")
            return None

        process = await asyncio.create_subprocess_exec(
            "git", "status", "-uno",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
            cwd=REPO_DIR
        )
        stdout, _ = await process.communicate()
        output = stdout.decode().lower()

        if "your branch is behind" in output:
            return True
        return False
    except Exception as e:
        logger.error(f"Error checking for updates: {e}")
        return None


async def perform_update(notify: Callable[[str], Coroutine]) -> bool:
    """Pulls changes and restarts the service."""
    logger.info("Update found! Pulling changes...")
    try:
        env_keys_before = _parse_env_keys(ENV_EXAMPLE_PATH)

        process = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
            cwd=REPO_DIR
        )
        stdout, _ = await process.communicate()
        old_rev = stdout.decode().strip()

        process = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
            cwd=REPO_DIR
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            logger.error("Git pull timed out")
            return False

        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"Git pull failed: {error_msg}")
            return False

        pip_warning = ""
        pip_path = os.path.join(REPO_DIR, ".venv", "bin", "pip")
        if os.path.exists(pip_path):
            logger.info("Updating dependencies via pip...")
            process = await asyncio.create_subprocess_exec(
                pip_path, "install", "-r", "requirements.txt",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=REPO_DIR
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout=300)
                if process.returncode != 0:
                    logger.warning("Pip install failed.")
                    pip_warning = "\n⚠️ <b>Warning</b>: Dependency update (pip) failed. You may need to run it manually."
            except asyncio.TimeoutError:
                process.kill()
                logger.error("Pip install timed out.")
                pip_warning = "\n⚠️ <b>Warning</b>: Dependency update (pip) timed out."
        else:
            logger.warning(f"Pip not found at {pip_path}, skipping dependency update.")

        process = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
            cwd=REPO_DIR
        )
        stdout, _ = await process.communicate()
        new_rev = stdout.decode().strip()

        if old_rev == new_rev:
            logger.info("Git pull completed but no changes were applied.")
            return False

        env_keys_after = _parse_env_keys(ENV_EXAMPLE_PATH)
        new_env_keys = env_keys_after - env_keys_before
        if new_env_keys:
            existing_user_keys = _parse_env_keys(ENV_PATH)
            missing_from_env = new_env_keys - existing_user_keys
            key_list = "\n".join(f"  • <code>{k}</code>" for k in sorted(new_env_keys))
            if missing_from_env:
                missing_list = "\n".join(f"  • <code>{k}</code>" for k in sorted(missing_from_env))
                env_notice = (
                    f"\n\n⚙️ <b>.env update required</b>\n"
                    f"New variable(s) added in <code>.env.example</code>:\n{key_list}\n\n"
                    f"The following are <b>not yet set</b> in your <code>.env</code>:\n{missing_list}\n"
                    f"Please add them before the bot restarts."
                )
            else:
                env_notice = (
                    f"\n\n⚙️ <b>.env.example updated</b>\n"
                    f"New variable(s) were added:\n{key_list}\n"
                    f"All appear to be set in your <code>.env</code> already."
                )
            logger.info(f"New .env.example keys detected: {new_env_keys}")
            await notify(env_notice)

        logger.info(f"Updated from {old_rev} to {new_rev}. Restarting service...")
        await notify(
            f"🚀 <b>Auto-update successful</b>\n"
            f"Updated from <code>{old_rev}</code> to <code>{new_rev}</code>."
            f"{pip_warning}\n"
            f"Restarting service..."
        )

        await asyncio.sleep(3)

        is_root = os.getuid() == 0
        env = os.environ.copy()
        if is_root:
            cmd = ["systemctl", "restart", "badgerbot"]
        else:
            cmd = ["systemctl", "--user", "restart", "badgerbot"]
            if "XDG_RUNTIME_DIR" not in env:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

        subprocess.Popen(cmd, env=env, start_new_session=True)
        return True
    except Exception as e:
        logger.error(f"Error performing update: {e}")
        return False


async def run_updater(
    interval_hours: int,
    stop_event: asyncio.Event,
    notify: Callable[[str], Coroutine]
):
    """Background task that periodically checks for and applies updates."""
    logger.info(f"Auto-updater initialized. Interval: {interval_hours} hour(s).")

    await asyncio.sleep(60)

    while not stop_event.is_set():
        try:
            update_status = None
            for attempt in range(5):
                update_status = await check_for_updates()
                if update_status is not None:
                    break

                if stop_event.is_set():
                    return

                wait_time = (2 ** attempt) * 300  # 5m, 10m, 20m, 40m, 80m
                logger.warning(f"Update check failed (outage?), retry {attempt+1}/5 in {wait_time/60:.0f}m...")
                await asyncio.sleep(wait_time)

            if update_status is None:
                logger.error("Update check failed after 5 attempts, giving up for this cycle.")
                await notify(
                    "⚠️ <b>Auto-update check failed</b> after 5 attempts. Will try again next cycle.\n\n"
                    "Check logs: <code>journalctl -u badgerbot -n 50</code>"
                )
            elif update_status is True:
                success = False
                for attempt in range(5):
                    if await perform_update(notify):
                        success = True
                        break

                    if stop_event.is_set():
                        return

                    wait_time = (2 ** attempt) * 300
                    logger.warning(f"Update execution failed, retry {attempt+1}/5 in {wait_time/60:.0f}m...")
                    await asyncio.sleep(wait_time)

                if success:
                    break
                else:
                    await notify(
                        "⚠️ <b>Auto-update failed</b> after 5 attempts. Will try again next cycle.\n\n"
                        "Check logs: <code>journalctl -u badgerbot -n 50</code>"
                    )

            for _ in range(interval_hours * 360):
                if stop_event.is_set():
                    break
                await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Updater loop error: {e}")
            await asyncio.sleep(60)
