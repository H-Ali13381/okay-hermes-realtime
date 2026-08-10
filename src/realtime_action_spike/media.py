"""Bounded YouTube resolution and controller-owned media playback primitives."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .contracts import YouTubePlayArguments, parse_exact_youtube_url


class MediaPlaybackError(RuntimeError):
    """A controlled media failure safe to report through the local tool contract."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class YouTubeVideo:
    video_id: str
    title: str
    channel: str
    webpage_url: str


PlaybackOutcome = Literal["completed", "failed"]


@dataclass(frozen=True, slots=True)
class PlaybackCompletion:
    session_id: str
    playback_id: str
    title: str
    channel: str
    outcome: PlaybackOutcome


class ResolverProcess(Protocol):
    returncode: int | None

    async def communicate(self) -> tuple[bytes, bytes]: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[ResolverProcess]]


async def _default_process_factory(*argv: str, **kwargs: Any) -> ResolverProcess:
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


class YouTubeResolver:
    """Resolve one allowlisted YouTube video through an argv-only yt-dlp subprocess."""

    def __init__(
        self,
        *,
        yt_dlp_bin: str = "yt-dlp",
        timeout_seconds: float = 12.0,
        terminate_timeout_seconds: float = 1.0,
        process_factory: ProcessFactory = _default_process_factory,
        browser_cookies: str | None = None,
    ) -> None:
        if timeout_seconds <= 0 or terminate_timeout_seconds <= 0:
            raise ValueError("resolver timeouts must be positive")
        self._yt_dlp_bin = yt_dlp_bin
        self._timeout_seconds = timeout_seconds
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self._process_factory = process_factory
        self._browser_cookies = browser_cookies.strip() if browser_cookies else None

    @property
    def yt_dlp_bin(self) -> str:
        return self._yt_dlp_bin

    @property
    def browser_cookies(self) -> str | None:
        return self._browser_cookies

    async def resolve(self, source: str) -> YouTubeVideo:
        arguments = YouTubePlayArguments.model_validate({"source": source})
        target = arguments.exact_url or f"ytsearch1:{arguments.source}"
        argv = [
            self._yt_dlp_bin,
            "--no-playlist",
            "--playlist-end",
            "1",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
        ]
        if self._browser_cookies:
            argv.extend(["--cookies-from-browser", self._browser_cookies])
        argv.extend(["--", target])

        try:
            process = await self._process_factory(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise MediaPlaybackError("yt-dlp is not installed or is not executable") from exc
        except OSError as exc:
            raise MediaPlaybackError("yt-dlp could not be started") from exc

        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            await self._terminate_and_reap(process)
            raise MediaPlaybackError("YouTube resolution timed out") from exc
        except asyncio.CancelledError:
            with contextlib.suppress(MediaPlaybackError):
                await asyncio.shield(self._terminate_and_reap(process))
            raise

        if process.returncode != 0:
            raise MediaPlaybackError("YouTube resolution failed")
        return _parse_resolver_output(stdout)

    async def _terminate_and_reap(self, process: ResolverProcess) -> None:
        if process.returncode is not None:
            with contextlib.suppress(Exception):
                await process.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_timeout_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._terminate_timeout_seconds,
            )
        except TimeoutError as exc:
            raise MediaPlaybackError("yt-dlp did not exit after forced termination") from exc


def _parse_resolver_output(stdout: bytes) -> YouTubeVideo:
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MediaPlaybackError("yt-dlp returned invalid metadata") from exc

    payload: Any = None
    for line in decoded.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        break
    if not isinstance(payload, dict):
        raise MediaPlaybackError("yt-dlp returned invalid metadata")

    entries = payload.get("entries")
    if isinstance(entries, list):
        payload = next((entry for entry in entries if isinstance(entry, dict)), None)
    if not isinstance(payload, dict):
        raise MediaPlaybackError("No YouTube video matched that request")

    video_id = payload.get("id")
    title = payload.get("title")
    channel = payload.get("channel") or payload.get("uploader")
    webpage_url = payload.get("webpage_url") or payload.get("original_url")
    if not isinstance(video_id, str) or not video_id.strip():
        raise MediaPlaybackError("yt-dlp returned incomplete metadata")
    if not isinstance(title, str) or not title.strip():
        raise MediaPlaybackError("yt-dlp returned incomplete metadata")
    if not isinstance(channel, str) or not channel.strip():
        raise MediaPlaybackError("yt-dlp returned incomplete metadata")
    if not isinstance(webpage_url, str) or not webpage_url.strip():
        raise MediaPlaybackError("yt-dlp returned incomplete metadata")

    try:
        returned_id, canonical_url = parse_exact_youtube_url(webpage_url)
    except ValueError as exc:
        raise MediaPlaybackError("yt-dlp returned an unsupported video") from exc
    if returned_id != video_id:
        raise MediaPlaybackError("yt-dlp returned inconsistent video metadata")

    return YouTubeVideo(
        video_id=video_id,
        title=" ".join(title.split())[:200],
        channel=" ".join(channel.split())[:120],
        webpage_url=canonical_url,
    )


class VideoResolver(Protocol):
    @property
    def yt_dlp_bin(self) -> str: ...

    @property
    def browser_cookies(self) -> str | None: ...

    async def resolve(self, source: str) -> YouTubeVideo: ...


class PlayerProcess(Protocol):
    pid: int

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...


PlayerProcessFactory = Callable[..., Awaitable[PlayerProcess]]
WaitForIpc = Callable[[Path, PlayerProcess, float], Awaitable[None]]
SendIpc = Callable[[Path, list[object], float], Awaitable[None]]
ExecutableResolver = Callable[[str], str | None]
ProcessGroupSignaler = Callable[[int, int], None]
CompletionHandler = Callable[[PlaybackCompletion], Awaitable[None]]


async def _default_player_process_factory(*argv: str, **kwargs: Any) -> PlayerProcess:
    return cast(PlayerProcess, await asyncio.create_subprocess_exec(*argv, **kwargs))


async def _default_send_ipc(path: Path, command: list[object], timeout: float) -> None:
    async def exchange() -> None:
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(json.dumps({"command": command}, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
            response_line = await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        try:
            response = json.loads(response_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MediaPlaybackError("mpv IPC returned an invalid response") from exc
        if not isinstance(response, dict) or response.get("error") != "success":
            raise MediaPlaybackError("mpv rejected the playback command")

    try:
        await asyncio.wait_for(exchange(), timeout=timeout)
    except TimeoutError as exc:
        raise MediaPlaybackError("mpv IPC command timed out") from exc
    except (ConnectionError, OSError) as exc:
        raise MediaPlaybackError("mpv IPC is unavailable") from exc


async def _default_wait_for_ipc(
    path: Path,
    process: PlayerProcess,
    timeout: float,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if process.returncode is not None:
            raise MediaPlaybackError("mpv exited before playback was prepared")
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise MediaPlaybackError("mpv IPC did not become ready")
        if path.exists():
            try:
                await _default_send_ipc(
                    path,
                    ["get_property", "pause"],
                    min(remaining, 0.25),
                )
                return
            except MediaPlaybackError:
                pass
        await asyncio.sleep(min(0.02, remaining))


def _default_process_group_signaler(pid: int, signal_number: int) -> None:
    os.killpg(pid, signal_number)


@dataclass(frozen=True, slots=True)
class PreparedPlayback:
    playback_id: str
    title: str
    channel: str
    webpage_url: str
    status: Literal["prepared"] = "prepared"

    def tool_result(self) -> dict[str, object]:
        return {
            "action": "media.youtube.play",
            "playback_id": self.playback_id,
            "title": self.title,
            "channel": self.channel,
            "webpage_url": self.webpage_url,
            "status": self.status,
            "end_session": True,
        }


@dataclass(slots=True)
class _OwnedPlayer:
    playback_id: str
    session_id: str
    video: YouTubeVideo
    process: PlayerProcess
    ipc_path: Path
    status: Literal["starting", "prepared", "playing"] = "starting"
    watcher: asyncio.Task[None] | None = None


class MediaPlaybackManager:
    """Own one paused/playing mpv generation and its private IPC lifecycle."""

    def __init__(
        self,
        *,
        resolver: VideoResolver | None = None,
        mpv_bin: str = "mpv",
        runtime_directory: Path | None = None,
        process_factory: PlayerProcessFactory = _default_player_process_factory,
        wait_for_ipc: WaitForIpc = _default_wait_for_ipc,
        send_ipc: SendIpc = _default_send_ipc,
        executable_resolver: ExecutableResolver = shutil.which,
        process_group_signaler: ProcessGroupSignaler = _default_process_group_signaler,
        playback_id_factory: Callable[[], str] | None = None,
        ipc_timeout_seconds: float = 2.0,
        process_exit_timeout_seconds: float = 1.5,
    ) -> None:
        if ipc_timeout_seconds <= 0 or process_exit_timeout_seconds <= 0:
            raise ValueError("media timeouts must be positive")
        self._resolver = resolver or YouTubeResolver()
        self._mpv_bin = mpv_bin
        self._runtime_directory = runtime_directory or self.default_runtime_directory()
        self._process_factory = process_factory
        self._wait_for_ipc = wait_for_ipc
        self._send_ipc = send_ipc
        self._executable_resolver = executable_resolver
        self._process_group_signaler = process_group_signaler
        self._playback_id_factory = playback_id_factory or (lambda: secrets.token_urlsafe(16))
        self._ipc_timeout_seconds = ipc_timeout_seconds
        self._process_exit_timeout_seconds = process_exit_timeout_seconds
        self._lock = asyncio.Lock()
        self._active: _OwnedPlayer | None = None
        self._completion_handler: CompletionHandler | None = None

    @staticmethod
    def default_runtime_directory() -> Path:
        runtime_root = os.getenv("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return Path(runtime_root) / "okay-hermes-realtime" / "media"

    @property
    def status(self) -> dict[str, object]:
        active = self._active
        if active is None:
            return {"status": "idle"}
        return {
            "status": active.status,
            "playback_id": active.playback_id,
            "title": active.video.title,
        }

    @property
    def active_ipc_path_for_testing(self) -> Path | None:
        active = self._active
        return active.ipc_path if active is not None else None

    async def prepare(self, session_id: str, source: str) -> PreparedPlayback:
        async with self._lock:
            mpv_path = self._resolve_executable(self._mpv_bin, "mpv")
            yt_dlp_path = self._resolve_executable(self._resolver.yt_dlp_bin, "yt-dlp")
            video = await self._resolver.resolve(source)
            if self._active is not None:
                await self._stop_active_locked()

            self._runtime_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._runtime_directory.chmod(0o700)
            playback_id = self._playback_id_factory()
            ipc_path = self._runtime_directory / f"{playback_id}.sock"
            ipc_path.unlink(missing_ok=True)
            argv = self._build_mpv_argv(
                mpv_path,
                yt_dlp_path,
                self._resolver.browser_cookies,
                ipc_path,
                video.webpage_url,
            )
            try:
                process = await self._process_factory(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise MediaPlaybackError("mpv is not installed or is not executable") from exc
            except OSError as exc:
                raise MediaPlaybackError("mpv could not be started") from exc

            player = _OwnedPlayer(
                playback_id=playback_id,
                session_id=session_id,
                video=video,
                process=process,
                ipc_path=ipc_path,
            )
            self._active = player
            try:
                await self._wait_for_ipc(ipc_path, process, self._ipc_timeout_seconds)
                if process.returncode is not None:
                    raise MediaPlaybackError("mpv exited before playback was prepared")
            except BaseException:
                await self._stop_active_locked()
                raise

            player.status = "prepared"
            player.watcher = asyncio.create_task(self._watch_player(player))
            return PreparedPlayback(
                playback_id=playback_id,
                title=video.title,
                channel=video.channel,
                webpage_url=video.webpage_url,
            )

    async def commit(self, session_id: str, playback_id: str) -> None:
        async with self._lock:
            player = self._active
            if (
                player is None
                or player.session_id != session_id
                or player.playback_id != playback_id
                or player.status != "prepared"
            ):
                raise MediaPlaybackError("stale or invalid prepared playback")
            await self._commit_active_locked(player)

    def bind_completion_handler(self, handler: CompletionHandler) -> None:
        if self._completion_handler is not None:
            raise MediaPlaybackError("completion handler already bound")
        self._completion_handler = handler

    async def finalize_session(
        self,
        session_id: str,
        *,
        playback_id: str,
        commit: bool,
    ) -> None:
        async with self._lock:
            player = self._active
            if (
                player is None
                or player.session_id != session_id
                or player.playback_id != playback_id
                or player.status != "prepared"
            ):
                return
            if commit:
                await self._commit_active_locked(player)
                return
            await self._stop_active_locked()

    async def stop_for_session(self, session_id: str, playback_id: str) -> None:
        async with self._lock:
            player = self._active
            if (
                player is not None
                and player.session_id == session_id
                and player.playback_id == playback_id
            ):
                await self._stop_active_locked()

    async def abort_prepared_for_session(self, session_id: str) -> None:
        async with self._lock:
            player = self._active
            if (
                player is not None
                and player.session_id == session_id
                and player.status == "prepared"
            ):
                await self._stop_active_locked()

    async def shutdown(self) -> None:
        async with self._lock:
            if self._active is not None:
                await self._stop_active_locked()

    def _resolve_executable(self, configured: str, display_name: str) -> str:
        resolved = self._executable_resolver(configured)
        if resolved is None:
            raise MediaPlaybackError(f"{display_name} is not installed or is not executable")
        return resolved

    @staticmethod
    def _build_mpv_argv(
        mpv_path: str,
        yt_dlp_path: str,
        browser_cookies: str | None,
        ipc_path: Path,
        webpage_url: str,
    ) -> tuple[str, ...]:
        quoted_yt_dlp_path = f"%{len(yt_dlp_path.encode('utf-8'))}%{yt_dlp_path}"
        argv = [
            mpv_path,
            "--no-config",
            "--no-terminal",
            "--really-quiet",
            "--force-window=yes",
            "--ontop",
            "--geometry=640x360-32-32",
            "--pause=yes",
            "--keep-open=no",
            "--idle=no",
            f"--script-opts=ytdl_hook-ytdl_path={quoted_yt_dlp_path}",
        ]
        if browser_cookies is not None:
            argv.append(
                "--ytdl-raw-options-append="
                f"cookies-from-browser={browser_cookies}"
            )
        argv.extend(
            [
                f"--input-ipc-server={ipc_path}",
                "--",
                webpage_url,
            ]
        )
        return tuple(argv)

    async def _watch_player(self, player: _OwnedPlayer) -> None:
        try:
            await player.process.wait()
        except asyncio.CancelledError:
            return

        completion: PlaybackCompletion | None = None
        handler: CompletionHandler | None = None
        async with self._lock:
            if self._active is not player:
                return
            player.watcher = None
            self._active = None
            self._cleanup_runtime(player.ipc_path)
            returncode = player.process.returncode if player.process.returncode is not None else 1
            completion = PlaybackCompletion(
                session_id=player.session_id,
                playback_id=player.playback_id,
                title=player.video.title,
                channel=player.video.channel,
                outcome="completed" if returncode == 0 else "failed",
            )
            handler = self._completion_handler

        if handler is None or completion is None:
            return
        try:
            await handler(completion)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("completion handler failed")

    async def _commit_active_locked(self, player: _OwnedPlayer) -> None:
        try:
            await self._send_ipc(
                player.ipc_path,
                ["set_property", "fullscreen", True],
                self._ipc_timeout_seconds,
            )
            await self._send_ipc(
                player.ipc_path,
                ["set_property", "pause", False],
                self._ipc_timeout_seconds,
            )
        except BaseException:
            await self._stop_active_locked()
            raise
        player.status = "playing"

    async def _stop_active_locked(self) -> None:
        player = self._active
        if player is None:
            return
        watcher = player.watcher
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        player.watcher = None
        try:
            await self._terminate_process(player.process)
        except BaseException:
            if player.process.returncode is None and self._active is player:
                player.watcher = asyncio.create_task(self._watch_player(player))
            raise
        if self._active is player:
            self._active = None
        self._cleanup_runtime(player.ipc_path)

    async def _terminate_process(self, process: PlayerProcess) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process_group_signaler(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_exit_timeout_seconds,
            )
            return
        except TimeoutError:
            pass
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process_group_signaler(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_exit_timeout_seconds,
            )
        except TimeoutError as exc:
            raise MediaPlaybackError("mpv did not exit after forced termination") from exc

    def _cleanup_runtime(self, ipc_path: Path) -> None:
        ipc_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self._runtime_directory.rmdir()
