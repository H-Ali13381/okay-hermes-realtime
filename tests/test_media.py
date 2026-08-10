from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from realtime_action_spike.media import (
    MediaPlaybackError,
    MediaPlaybackManager,
    PlaybackCompletion,
    YouTubePlayArguments,
    YouTubeResolver,
    YouTubeVideo,
)


class FakeResolverProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        block: bool = False,
        stubborn: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None if block else returncode
        self._final_returncode = returncode
        self._release = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.stubborn = stubborn
        if not block:
            self._release.set()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._release.wait()
        self.returncode = self._final_returncode
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.stubborn:
            return
        self.returncode = -15
        self._release.set()

    def kill(self) -> None:
        self.kill_calls += 1
        if self.stubborn:
            return
        self.returncode = -9
        self._release.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._release.wait()
        assert self.returncode is not None
        return self.returncode


class RecordingProcessFactory:
    def __init__(self, process: FakeResolverProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeResolverProcess:
        self.calls.append((argv, kwargs))
        return self.process


@pytest.mark.parametrize(
    "source",
    [
        "latest Technology Connections video",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
    ],
)
def test_youtube_arguments_accept_searches_and_allowlisted_urls(source: str) -> None:
    parsed = YouTubePlayArguments.model_validate({"source": f"  {source}  "})

    assert parsed.source == source


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "at least 1 character"),
        ("x" * 301, "at most 300 characters"),
        ("play\nthis", "control characters"),
        ("https://example.com/watch?v=dQw4w9WgXcQ", "YouTube URL"),
        ("example.com/watch?v=dQw4w9WgXcQ", "YouTube URL"),
        ("youtube.com/watch?v=dQw4w9WgXcQ", "YouTube URL"),
        ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", "https"),
        ("https://youtube.com:8443/watch?v=dQw4w9WgXcQ", "standard https port"),
        ("https://www.youtube.com/playlist?list=PL123", "supported YouTube video URL"),
    ],
)
def test_youtube_arguments_reject_unsafe_sources(source: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        YouTubePlayArguments.model_validate({"source": source})


def test_youtube_arguments_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        YouTubePlayArguments.model_validate({"source": "cats", "flags": "--ytdl-format=best"})


@pytest.mark.asyncio
async def test_resolver_uses_one_result_search_without_a_shell() -> None:
    process = FakeResolverProcess(
        stdout=(
            json.dumps(
                {
                    "id": "dQw4w9WgXcQ",
                    "title": "A useful video",
                    "channel": "Useful Channel",
                    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                }
            ).encode()
            + b"\n"
        )
    )
    factory = RecordingProcessFactory(process)
    resolver = YouTubeResolver(
        yt_dlp_bin="/usr/bin/yt-dlp",
        timeout_seconds=1.0,
        process_factory=factory,
    )

    result = await resolver.resolve("latest useful channel video")

    assert result.video_id == "dQw4w9WgXcQ"
    assert result.title == "A useful video"
    assert result.channel == "Useful Channel"
    assert result.webpage_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    argv, kwargs = factory.calls[0]
    assert argv[0] == "/usr/bin/yt-dlp"
    assert "ytsearch1:latest useful channel video" in argv
    assert "--playlist-end" in argv
    assert argv[argv.index("--playlist-end") + 1] == "1"
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert "shell" not in kwargs


@pytest.mark.asyncio
async def test_resolver_preserves_an_exact_allowlisted_url() -> None:
    url = "https://youtu.be/dQw4w9WgXcQ"
    process = FakeResolverProcess(
        stdout=json.dumps(
            {
                "id": "dQw4w9WgXcQ",
                "title": "Exact result",
                "uploader": "Uploader",
                "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            }
        ).encode()
    )
    factory = RecordingProcessFactory(process)
    resolver = YouTubeResolver(process_factory=factory)

    result = await resolver.resolve(url)

    assert result.channel == "Uploader"
    assert factory.calls[0][0][-1] == url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        json.dumps({"entries": []}).encode(),
        json.dumps(
            {
                "id": "not-a-video-id",
                "title": "Bad",
                "channel": "Bad",
                "webpage_url": "https://example.com/watch?v=not-a-video-id",
            }
        ).encode(),
    ],
)
async def test_resolver_rejects_missing_or_malformed_results(payload: bytes) -> None:
    resolver = YouTubeResolver(
        process_factory=RecordingProcessFactory(FakeResolverProcess(stdout=payload))
    )

    with pytest.raises(MediaPlaybackError):
        await resolver.resolve("something unlikely")


@pytest.mark.asyncio
async def test_resolver_timeout_terminates_and_reaps_the_exact_process() -> None:
    process = FakeResolverProcess(block=True)
    resolver = YouTubeResolver(
        timeout_seconds=0.001,
        terminate_timeout_seconds=0.1,
        process_factory=RecordingProcessFactory(process),
    )

    with pytest.raises(MediaPlaybackError, match="timed out"):
        await resolver.resolve("slow result")

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_resolver_cleanup_is_bounded_when_process_ignores_kill() -> None:
    process = FakeResolverProcess(block=True, stubborn=True)
    resolver = YouTubeResolver(
        timeout_seconds=0.001,
        terminate_timeout_seconds=0.001,
        process_factory=RecordingProcessFactory(process),
    )

    with pytest.raises(MediaPlaybackError, match="forced termination"):
        await asyncio.wait_for(resolver.resolve("stubborn result"), timeout=0.1)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


@pytest.mark.asyncio
async def test_resolver_cancellation_terminates_and_reaps_exact_process() -> None:
    process = FakeResolverProcess(block=True)
    resolver = YouTubeResolver(
        timeout_seconds=1.0,
        terminate_timeout_seconds=0.1,
        process_factory=RecordingProcessFactory(process),
    )
    resolution = asyncio.create_task(resolver.resolve("cancelled result"))
    await asyncio.sleep(0)

    resolution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resolution

    assert process.terminate_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_resolver_reports_bounded_errors_without_leaking_stderr() -> None:
    secret = "signed-cookie-value-that-must-not-leak"
    process = FakeResolverProcess(stderr=secret.encode(), returncode=1)
    resolver = YouTubeResolver(
        process_factory=RecordingProcessFactory(process),
    )

    with pytest.raises(MediaPlaybackError) as caught:
        await resolver.resolve("challenged result")

    assert secret not in str(caught.value)
    assert len(str(caught.value)) < 200


class StubResolver:
    def __init__(self, videos: list[YouTubeVideo]) -> None:
        self.videos = list(videos)
        self.sources: list[str] = []
        self.yt_dlp_bin = "/usr/bin/yt-dlp"
        self.browser_cookies: str | None = None

    async def resolve(self, source: str) -> YouTubeVideo:
        self.sources.append(source)
        return self.videos.pop(0)


class FakePlayerProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.wait_calls = 0
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def exit(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._exited.set()


class RecordingPlayerFactory:
    def __init__(self, processes: list[FakePlayerProcess]) -> None:
        self.processes = list(processes)
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakePlayerProcess:
        self.calls.append((argv, kwargs))
        return self.processes.pop(0)


class RecordingIpc:
    def __init__(
        self,
        *,
        fail_send: bool = False,
        fail_on_command: int | None = None,
        exit_on_ready: bool = False,
    ) -> None:
        self.ready_paths: list[Path] = []
        self.commands: list[tuple[Path, list[object]]] = []
        self.fail_send = fail_send
        self.fail_on_command = fail_on_command
        self.exit_on_ready = exit_on_ready

    async def wait_ready(
        self,
        path: Path,
        process: Any,
        timeout: float,
    ) -> None:
        del timeout
        self.ready_paths.append(path)
        path.touch()
        if self.exit_on_ready:
            process.exit(1)

    async def send(self, path: Path, command: list[object], timeout: float) -> None:
        del timeout
        self.commands.append((path, command))
        if self.fail_send or self.fail_on_command == len(self.commands) - 1:
            raise MediaPlaybackError("simulated IPC failure")


class RecordingGroupSignaler:
    def __init__(self, processes: list[FakePlayerProcess]) -> None:
        self.processes = {process.pid: process for process in processes}
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, signal_number: int) -> None:
        self.calls.append((pid, signal_number))
        self.processes[pid].exit(-signal_number)


class StubbornGroupSignaler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, signal_number: int) -> None:
        self.calls.append((pid, signal_number))


def _video(video_id: str, title: str) -> YouTubeVideo:
    return YouTubeVideo(
        video_id=video_id,
        title=title,
        channel="Channel",
        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _manager(
    tmp_path: Path,
    *,
    videos: list[YouTubeVideo],
    processes: list[FakePlayerProcess],
    ipc: RecordingIpc | None = None,
    executable_resolver=lambda command: f"/usr/bin/{Path(command).name}",
) -> tuple[
    MediaPlaybackManager,
    StubResolver,
    RecordingPlayerFactory,
    RecordingIpc,
    RecordingGroupSignaler,
]:
    resolver = StubResolver(videos)
    factory = RecordingPlayerFactory(processes)
    ipc = ipc or RecordingIpc()
    signaler = RecordingGroupSignaler(processes)
    playback_ids = iter(["playback-1", "playback-2", "playback-3"])
    manager = MediaPlaybackManager(
        resolver=resolver,
        mpv_bin="mpv",
        runtime_directory=tmp_path / "media",
        process_factory=factory,
        wait_for_ipc=ipc.wait_ready,
        send_ipc=ipc.send,
        executable_resolver=executable_resolver,
        process_group_signaler=signaler,
        playback_id_factory=lambda: next(playback_ids),
        ipc_timeout_seconds=0.2,
        process_exit_timeout_seconds=0.2,
    )
    return manager, resolver, factory, ipc, signaler


@pytest.mark.asyncio
async def test_playback_prepare_uses_fixed_paused_mpv_policy_and_private_runtime_dir(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31001)
    manager, resolver, factory, ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    resolver.browser_cookies = "brave:Default"

    prepared = await manager.prepare("voice-session-1", "latest useful video")

    assert prepared.playback_id == "playback-1"
    assert prepared.title == "Selected title"
    assert prepared.status == "prepared"
    assert resolver.sources == ["latest useful video"]
    argv, kwargs = factory.calls[0]
    assert argv[0] == "/usr/bin/mpv"
    assert "--pause=yes" in argv
    assert "--ontop" in argv
    assert "--no-terminal" in argv
    assert "--fullscreen" not in argv
    assert "--no-playlist" not in argv
    assert "--geometry=640x360-32-32" in argv
    assert "--script-opts=ytdl_hook-ytdl_path=%15%/usr/bin/yt-dlp" in argv
    assert "--ytdl-raw-options-append=cookies-from-browser=brave:Default" in argv
    assert argv[-2:] == ("--", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert not any("latest useful video" in argument for argument in argv)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert ipc.ready_paths
    assert (tmp_path / "media").stat().st_mode & 0o777 == 0o700
    assert manager.status == {
        "status": "prepared",
        "playback_id": "playback-1",
        "title": "Selected title",
    }

    await manager.shutdown()


@pytest.mark.asyncio
async def test_player_exit_at_ipc_readiness_is_not_reported_as_prepared(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31013)
    ipc = RecordingIpc(exit_on_ready=True)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
        ipc=ipc,
    )

    with pytest.raises(MediaPlaybackError, match="exited before playback was prepared"):
        await manager.prepare("voice-session-1", "video")

    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_commit_unpauses_only_the_exact_prepared_session_and_generation(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31002)
    manager, _resolver, _factory, ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    prepared = await manager.prepare("voice-session-1", "video")

    with pytest.raises(MediaPlaybackError, match="stale"):
        await manager.commit("other-session", prepared.playback_id)
    with pytest.raises(MediaPlaybackError, match="stale"):
        await manager.commit("voice-session-1", "older-playback")
    assert ipc.commands == []

    await manager.commit("voice-session-1", prepared.playback_id)

    assert ipc.commands == [
        (
            ipc.ready_paths[0],
            ["set_property", "fullscreen", True],
        ),
        (
            ipc.ready_paths[0],
            ["set_property", "pause", False],
        ),
    ]
    assert manager.status["status"] == "playing"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_session_finalization_aborts_paused_player_and_cleans_runtime(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31003)
    manager, _resolver, _factory, _ipc, signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    prepared = await manager.prepare("voice-session-1", "video")
    ipc_path = manager.active_ipc_path_for_testing
    assert ipc_path is not None and ipc_path.exists()

    await manager.finalize_session(
        "voice-session-1",
        playback_id=prepared.playback_id,
        commit=False,
    )

    assert signaler.calls[0][0] == process.pid
    assert manager.status == {"status": "idle"}
    assert not ipc_path.exists()


@pytest.mark.asyncio
async def test_commit_failure_aborts_and_reaps_paused_player(tmp_path: Path) -> None:
    process = FakePlayerProcess(31011)
    ipc = RecordingIpc(fail_on_command=0)
    manager, _resolver, _factory, _ipc, signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
        ipc=ipc,
    )
    prepared = await manager.prepare("voice-session-1", "video")

    with pytest.raises(MediaPlaybackError, match="simulated IPC failure"):
        await manager.finalize_session(
            "voice-session-1",
            playback_id=prepared.playback_id,
            commit=True,
        )

    assert ipc.commands == [
        (
            ipc.ready_paths[0],
            ["set_property", "fullscreen", True],
        )
    ]
    assert signaler.calls == [(process.pid, signal.SIGTERM)]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_commit_failure_keeps_exact_stubborn_player_owned_until_explicit_exit(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31012)
    resolver = StubResolver([_video("dQw4w9WgXcQ", "Selected title")])
    factory = RecordingPlayerFactory([process])
    ipc = RecordingIpc(fail_on_command=0)
    signaler = StubbornGroupSignaler()
    playback_ids = iter(["playback-1"])
    manager = MediaPlaybackManager(
        resolver=resolver,
        mpv_bin="mpv",
        runtime_directory=tmp_path / "media",
        process_factory=factory,
        wait_for_ipc=ipc.wait_ready,
        send_ipc=ipc.send,
        executable_resolver=lambda command: f"/usr/bin/{Path(command).name}",
        process_group_signaler=signaler,
        playback_id_factory=lambda: next(playback_ids),
        ipc_timeout_seconds=0.01,
        process_exit_timeout_seconds=0.01,
    )
    prepared = await manager.prepare("voice-session-1", "video")
    ipc_path = manager.active_ipc_path_for_testing
    assert ipc_path is not None and ipc_path.exists()

    with pytest.raises(MediaPlaybackError, match="forced termination"):
        await asyncio.wait_for(
            manager.commit("voice-session-1", prepared.playback_id),
            timeout=0.2,
        )

    assert ipc.commands == [
        (
            ipc.ready_paths[0],
            ["set_property", "fullscreen", True],
        )
    ]
    assert signaler.calls == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert manager.status == {
        "status": "prepared",
        "playback_id": prepared.playback_id,
        "title": "Selected title",
    }
    assert ipc_path.exists()

    process.exit(0)
    await manager.shutdown()

    assert manager.status == {"status": "idle"}
    assert not ipc_path.exists()


@pytest.mark.asyncio
async def test_new_request_replaces_and_reaps_old_owned_player(
    tmp_path: Path,
) -> None:
    old = FakePlayerProcess(31004)
    new = FakePlayerProcess(31005)
    manager, _resolver, _factory, _ipc, signaler = _manager(
        tmp_path,
        videos=[
            _video("dQw4w9WgXcQ", "First"),
            _video("aqz-KE-bpKQ", "Second"),
        ],
        processes=[old, new],
    )

    first = await manager.prepare("voice-session-1", "first")
    second = await manager.prepare("voice-session-1", "second")

    assert first.playback_id == "playback-1"
    assert second.playback_id == "playback-2"
    assert signaler.calls[0][0] == old.pid
    assert old.wait_calls >= 1
    assert manager.status["title"] == "Second"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_replacement_keeps_exact_stubborn_player_owned(
    tmp_path: Path,
) -> None:
    old = FakePlayerProcess(31011)
    new = FakePlayerProcess(31012)
    resolver = StubResolver(
        [
            _video("dQw4w9WgXcQ", "First"),
            _video("aqz-KE-bpKQ", "Second"),
        ]
    )
    factory = RecordingPlayerFactory([old, new])
    ipc = RecordingIpc()
    signaler = StubbornGroupSignaler()
    playback_ids = iter(["playback-1", "playback-2"])
    manager = MediaPlaybackManager(
        resolver=resolver,
        mpv_bin="mpv",
        runtime_directory=tmp_path / "media",
        process_factory=factory,
        wait_for_ipc=ipc.wait_ready,
        send_ipc=ipc.send,
        executable_resolver=lambda command: f"/usr/bin/{Path(command).name}",
        process_group_signaler=signaler,
        playback_id_factory=lambda: next(playback_ids),
        ipc_timeout_seconds=0.01,
        process_exit_timeout_seconds=0.01,
    )
    await manager.prepare("voice-session-1", "first")

    with pytest.raises(MediaPlaybackError, match="forced termination"):
        await asyncio.wait_for(
            manager.prepare("voice-session-1", "second"),
            timeout=0.2,
        )

    assert len(factory.calls) == 1
    assert manager.status["title"] == "First"
    assert len(signaler.calls) == 2
    old.exit(-9)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_stale_player_exit_cannot_clear_newer_playback_state(tmp_path: Path) -> None:
    old = FakePlayerProcess(31006)
    new = FakePlayerProcess(31007)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[
            _video("dQw4w9WgXcQ", "First"),
            _video("aqz-KE-bpKQ", "Second"),
        ],
        processes=[old, new],
    )

    await manager.prepare("voice-session-1", "first")
    await manager.prepare("voice-session-1", "second")
    old.exit(0)
    await asyncio.sleep(0)

    assert manager.status["title"] == "Second"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_player_normal_exit_releases_only_matching_runtime_state(tmp_path: Path) -> None:
    process = FakePlayerProcess(31008)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected")],
        processes=[process],
    )
    await manager.prepare("voice-session-1", "video")
    ipc_path = manager.active_ipc_path_for_testing
    assert ipc_path is not None

    process.exit(0)
    for _ in range(10):
        if manager.status == {"status": "idle"}:
            break
        await asyncio.sleep(0)

    assert manager.status == {"status": "idle"}
    assert not ipc_path.exists()


def test_playback_completion_is_frozen_and_slot_based() -> None:
    completion = PlaybackCompletion(
        session_id="voice-session-1",
        playback_id="playback-1",
        title="Selected",
        channel="Channel",
        outcome="completed",
    )

    assert completion.__slots__ == (
        "session_id",
        "playback_id",
        "title",
        "channel",
        "outcome",
    )
    with pytest.raises(FrozenInstanceError):
        completion.outcome = "failed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_committed_player_exit_notifies_once_after_state_clears_and_handler_can_reenter(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31014)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    completions: list[PlaybackCompletion] = []
    entered = asyncio.Event()
    released = asyncio.Event()

    async def handler(completion: PlaybackCompletion) -> None:
        completions.append(completion)
        assert completion == PlaybackCompletion(
            session_id="voice-session-1",
            playback_id="playback-1",
            title="Selected title",
            channel="Channel",
            outcome="completed",
        )
        assert manager.status == {"status": "idle"}
        assert manager.active_ipc_path_for_testing is None
        entered.set()
        await asyncio.wait_for(manager.shutdown(), timeout=0.1)
        released.set()

    manager.bind_completion_handler(handler)
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    process.exit(0)
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    await asyncio.wait_for(released.wait(), timeout=0.2)

    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id=prepared.playback_id,
            title="Selected title",
            channel="Channel",
            outcome="completed",
        )
    ]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_committed_player_exit_reports_failed_outcome_when_process_exits_nonzero(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31015)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    completions: list[PlaybackCompletion] = []

    async def handler(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_completion_handler(handler)
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    process.exit(1)
    for _ in range(20):
        if completions:
            break
        await asyncio.sleep(0)

    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id=prepared.playback_id,
            title="Selected title",
            channel="Channel",
            outcome="failed",
        )
    ]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["shutdown", "stop_for_session"])
async def test_shutdown_and_exact_stop_for_session_do_not_notify_completion(
    tmp_path: Path,
    action: str,
) -> None:
    process = FakePlayerProcess(31016)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    completions: list[PlaybackCompletion] = []

    async def handler(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_completion_handler(handler)
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    if action == "shutdown":
        await manager.shutdown()
    else:
        await manager.stop_for_session("voice-session-1", prepared.playback_id)

    await asyncio.sleep(0)

    assert completions == []
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_stop_for_session_ignores_stale_session_or_playback_id(tmp_path: Path) -> None:
    process = FakePlayerProcess(31017)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    completions: list[PlaybackCompletion] = []

    async def handler(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_completion_handler(handler)
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    await manager.stop_for_session("stale-session", prepared.playback_id)
    await manager.stop_for_session("voice-session-1", "older-playback")

    assert completions == []
    assert manager.status["status"] == "playing"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_stale_player_exit_does_not_notify_or_clear_newer_generation(tmp_path: Path) -> None:
    old = FakePlayerProcess(31018)
    new = FakePlayerProcess(31019)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[
            _video("dQw4w9WgXcQ", "First"),
            _video("aqz-KE-bpKQ", "Second"),
        ],
        processes=[old, new],
    )
    completions: list[PlaybackCompletion] = []

    async def handler(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_completion_handler(handler)

    await manager.prepare("voice-session-1", "first")
    await manager.prepare("voice-session-1", "second")
    old.exit(0)
    await asyncio.sleep(0)

    assert completions == []
    assert manager.status["title"] == "Second"

    new.exit(0)
    for _ in range(20):
        if completions:
            break
        await asyncio.sleep(0)

    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id="playback-2",
            title="Second",
            channel="Channel",
            outcome="completed",
        )
    ]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_handler_exception_is_logged_and_state_stays_cleared(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    process = FakePlayerProcess(31020)
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )

    async def handler(_completion: PlaybackCompletion) -> None:
        raise RuntimeError("boom")

    manager.bind_completion_handler(handler)
    prepared = await manager.prepare("voice-session-1", "video")

    with caplog.at_level(logging.ERROR, logger="realtime_action_spike.media"):
        await manager.commit("voice-session-1", prepared.playback_id)
        process.exit(0)
        for _ in range(20):
            if manager.status == {"status": "idle"}:
                break
            await asyncio.sleep(0)

    assert manager.status == {"status": "idle"}
    assert any("completion handler failed" in record.message for record in caplog.records)


def test_binding_a_second_completion_handler_is_rejected() -> None:
    manager = MediaPlaybackManager()

    async def handler(_completion: PlaybackCompletion) -> None:
        return None

    manager.bind_completion_handler(handler)

    with pytest.raises(MediaPlaybackError, match="already bound"):
        manager.bind_completion_handler(handler)


@pytest.mark.asyncio
async def test_missing_prerequisite_fails_before_resolution_or_spawn(tmp_path: Path) -> None:
    process = FakePlayerProcess(31009)
    manager, resolver, factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected")],
        processes=[process],
        executable_resolver=lambda command: None if Path(command).name == "mpv" else command,
    )

    with pytest.raises(MediaPlaybackError, match="mpv"):
        await manager.prepare("voice-session-1", "video")

    assert resolver.sources == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_shutdown_reaps_playing_player_and_is_idempotent(tmp_path: Path) -> None:
    process = FakePlayerProcess(31010)
    manager, _resolver, _factory, _ipc, signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected")],
        processes=[process],
    )
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    await manager.shutdown()
    await manager.shutdown()

    assert [pid for pid, _signal in signaler.calls] == [process.pid]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_real_subprocess_ipc_prepare_commit_and_process_group_shutdown(
    tmp_path: Path,
) -> None:
    fake_mpv = tmp_path / "fake-mpv"
    fake_mpv.write_text(
        """#!/usr/bin/env python3
import json
import socket
import sys

ipc_arg = next(arg for arg in sys.argv if arg.startswith('--input-ipc-server='))
ipc_path = ipc_arg.split('=', 1)[1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(ipc_path)
server.listen(4)
while True:
    connection, _ = server.accept()
    with connection:
        request = b''
        while not request.endswith(b'\\n'):
            chunk = connection.recv(4096)
            if not chunk:
                break
            request += chunk
        json.loads(request)
        connection.sendall(b'{\"error\":\"success\",\"data\":true}\\n')
""",
        encoding="utf-8",
    )
    fake_mpv.chmod(0o755)
    resolver = StubResolver([_video("dQw4w9WgXcQ", "Selected")])
    resolver.yt_dlp_bin = sys.executable
    manager = MediaPlaybackManager(
        resolver=resolver,
        mpv_bin=str(fake_mpv),
        runtime_directory=tmp_path / "runtime" / "media",
        ipc_timeout_seconds=1.0,
        process_exit_timeout_seconds=1.0,
    )

    prepared = await manager.prepare("voice-session-real", "video")
    await manager.commit("voice-session-real", prepared.playback_id)

    assert manager.status["status"] == "playing"
    await manager.shutdown()
    assert manager.status == {"status": "idle"}
    assert list((tmp_path / "runtime").glob("media/*.sock")) == []
