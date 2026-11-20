from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from realtime_action_spike.contracts import MediaPrepareYouTubeArguments
from realtime_action_spike.media import (
    MediaPlaybackError,
    MediaPlaybackManager,
    PlaybackCompletion,
    PlaybackProgress,
    YouTubeResolver,
    YouTubeVideo,
)
from realtime_action_spike.media_catalog import DirectStreamLocator, YouTubePlaybackLocator


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    # AF_UNIX socket paths must stay under the platform limit even when pytest
    # uses a long basetemp path.
    with tempfile.TemporaryDirectory(prefix="ohv-media-") as directory:
        yield Path(directory)


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
    parsed = MediaPrepareYouTubeArguments.model_validate({"query": f"  {source}  "})

    assert parsed.query == source


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "at least 1 character"),
        ("x" * 301, "at most 300 characters"),
        ("play\nthis", "control characters"),
        ("https://example.com/watch?v=dQw4w9WgXcQ", "YouTube URL"),
        ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", "https"),
        ("https://youtube.com:8443/watch?v=dQw4w9WgXcQ", "standard https port"),
        ("https://www.youtube.com/playlist?list=PL123", "supported YouTube video URL"),
    ],
)
def test_youtube_arguments_reject_unsafe_sources(source: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MediaPrepareYouTubeArguments.model_validate({"query": source})


def test_youtube_arguments_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MediaPrepareYouTubeArguments.model_validate(
            {"query": "cats", "flags": "--ytdl-format=best"}
        )


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
async def test_resolver_requests_android_player_client_for_playable_urls() -> None:
    process = FakeResolverProcess(
        stdout=json.dumps(
            {
                "id": "dQw4w9WgXcQ",
                "title": "A useful video",
                "channel": "Useful Channel",
                "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            }
        ).encode()
    )
    factory = RecordingProcessFactory(process)
    resolver = YouTubeResolver(process_factory=factory)

    await resolver.resolve("latest useful channel video")

    argv = factory.calls[0][0]
    assert "--extractor-args" in argv
    assert argv[argv.index("--extractor-args") + 1] == "youtube:player_client=android"


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
        property_values: list[tuple[float, float]] | None = None,
    ) -> None:
        self.ready_paths: list[Path] = []
        self.commands: list[tuple[Path, list[object]]] = []
        self.fail_send = fail_send
        self.fail_on_command = fail_on_command
        self.exit_on_ready = exit_on_ready
        self.property_values = list(property_values or [])
        self.property_reads: list[tuple[Path, str]] = []

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

    async def read(self, path: Path, property_name: str, timeout: float) -> float:
        del timeout
        self.property_reads.append((path, property_name))
        if not self.property_values:
            raise MediaPlaybackError("simulated IPC property failure")
        position, duration = self.property_values[0]
        value = position if property_name == "time-pos" else duration
        if property_name == "duration":
            self.property_values.pop(0)
        return value


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
    progress_interval_seconds: float = 15.0,
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
        read_ipc=ipc.read,
        executable_resolver=executable_resolver,
        process_group_signaler=signaler,
        playback_id_factory=lambda: next(playback_ids),
        ipc_timeout_seconds=0.2,
        process_exit_timeout_seconds=0.2,
        progress_interval_seconds=progress_interval_seconds,
    )
    return manager, resolver, factory, ipc, signaler


async def _launch_confirmed(
    manager: MediaPlaybackManager,
    *,
    playback_id: str = "playback-1",
    title: str = "Selected title",
    session_id: str = "voice-session-1",
) -> None:
    await manager.launch(
        session_id=session_id,
        playback_id=playback_id,
        locator=DirectStreamLocator(
            stream_url="https://hls.anidb.app/stream/example/ep.m3u8"
        ),
        title=title,
        subtitle="Channel",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locator", "expected_target", "needs_ytdlp"),
    [
        (
            YouTubePlaybackLocator(
                video_id="dQw4w9WgXcQ",
                canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            True,
        ),
        (
            DirectStreamLocator(stream_url="https://hls.anidb.app/stream/example/ep.m3u8"),
            "https://hls.anidb.app/stream/example/ep.m3u8",
            False,
        ),
    ],
)
async def test_launch_starts_confirmed_locator_fullscreen_unpaused_once(
    tmp_path: Path,
    locator: object,
    expected_target: str,
    needs_ytdlp: bool,
) -> None:
    process = FakePlayerProcess(31100)
    manager, resolver, factory, ipc, _signaler = _manager(
        tmp_path,
        videos=[],
        processes=[process],
        executable_resolver=lambda command: (
            None
            if Path(command).name == "yt-dlp" and not needs_ytdlp
            else f"/usr/bin/{Path(command).name}"
        ),
    )

    launched = await manager.launch(
        session_id="voice-session-1",
        playback_id="pending-accepted-1",
        locator=locator,
        title="Confirmed title",
        subtitle="Confirmed subtitle",
        start_seconds=93.5,
    )

    assert launched.playback_id == "pending-accepted-1"
    assert launched.status == "playing"
    assert resolver.sources == []
    assert len(factory.calls) == 1
    argv, kwargs = factory.calls[0]
    assert "--fullscreen=yes" in argv
    assert "--pause=no" in argv
    assert "--start=93.500" in argv
    assert "--geometry=640x360-32-32" not in argv
    assert argv[-2:] == ("--", expected_target)
    assert any("ytdl_hook-ytdl_path" in arg for arg in argv) is needs_ytdlp
    assert (
        "--ytdl-raw-options-append=extractor-args=youtube:player_client=android" in argv
    ) is needs_ytdlp
    assert kwargs["start_new_session"] is True
    assert ipc.ready_paths
    assert manager.status == {
        "status": "playing",
        "playback_id": "pending-accepted-1",
        "title": "Confirmed title",
    }
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["shutdown", "stop_for_session", "exit"])
async def test_launch_keeps_http_headers_in_private_config_and_cleans_up(
    tmp_path: Path, cleanup: str,
) -> None:
    process = FakePlayerProcess(31200)
    manager, _resolver, factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[],
        processes=[process],
        executable_resolver=lambda command: f"/usr/bin/{Path(command).name}",
    )
    locator = DirectStreamLocator(
        stream_url="https://cdn.example.test/video/manifest.mpd",
        http_headers=(
            ("Authorization", "Bearer token-value"),
            ("Referer", "https://www.example.test/"),
        ),
    )

    await manager.launch(
        session_id="voice-session-headers",
        playback_id="pending-accepted-headers",
        locator=locator,
        title="Confirmed title",
        subtitle="Confirmed subtitle",
    )

    argv, _kwargs = factory.calls[0]
    assert not any(value in arg for arg in argv for _name, value in locator.http_headers)
    assert not any(arg.startswith("--http-header-fields") for arg in argv)
    config_path = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--include=")))
    assert config_path.parent == tmp_path / "media"
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert config_path.parent.stat().st_mode & 0o777 == 0o700
    expected_header = ",".join(f"{name}: {value}" for name, value in locator.http_headers)
    quoted_header = f"%{len(expected_header.encode('utf-8'))}%{expected_header}"
    assert config_path.read_text() == f"http-header-fields={quoted_header}\n"

    if cleanup == "exit":
        completed = asyncio.Event()

        async def on_complete(_completion: PlaybackCompletion) -> None:
            completed.set()

        manager.bind_completion_handler(on_complete)
        process.exit()
        await asyncio.wait_for(completed.wait(), timeout=0.2)
    elif cleanup == "stop_for_session":
        await manager.stop_for_session("voice-session-headers", "pending-accepted-headers")
    else:
        await manager.shutdown()
    assert not config_path.exists()
    assert not config_path.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "cancel", "ipc"])
async def test_http_header_config_is_cleaned_up_on_launch_failure(
    tmp_path: Path, failure: str,
) -> None:
    process = FakePlayerProcess(31201)
    manager, _resolver, factory, _ipc, _signaler = _manager(
        tmp_path, videos=[], processes=[process], ipc=RecordingIpc(exit_on_ready=True),
    )
    config_paths: list[Path] = []

    async def failing_factory(*argv: str, **kwargs: Any) -> FakePlayerProcess:
        config_path = Path(
            next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--include="))
        )
        config_paths.append(config_path)
        assert config_path.exists()
        if failure == "spawn":
            raise OSError("simulated spawn failure")
        if failure == "cancel":
            raise asyncio.CancelledError
        return await factory(*argv, **kwargs)

    manager._process_factory = failing_factory
    expected_error = asyncio.CancelledError if failure == "cancel" else MediaPlaybackError
    with pytest.raises(expected_error):
        await manager.launch(
            session_id="voice-session-headers",
            playback_id="pending-accepted-headers",
            locator=DirectStreamLocator(
                stream_url="https://cdn.example.test/video/manifest.mpd",
                http_headers=(("Authorization", "Bearer test-only-placeholder"),),
            ),
            title="Confirmed title",
            subtitle="Confirmed subtitle",
        )
    assert len(config_paths) == 1
    assert not config_paths[0].exists()
    assert not (tmp_path / "media").exists()
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_duplicate_launch_generation_never_spawns_second_player(tmp_path: Path) -> None:
    process = FakePlayerProcess(31101)
    manager, _resolver, factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[],
        processes=[process],
    )
    locator = DirectStreamLocator(
        stream_url="https://hls.anidb.app/stream/example/ep.m3u8"
    )
    await manager.launch(
        session_id="voice-session-1",
        playback_id="pending-accepted-1",
        locator=locator,
        title="Confirmed title",
        subtitle="Confirmed subtitle",
    )

    with pytest.raises(MediaPlaybackError, match="already active"):
        await manager.launch(
            session_id="voice-session-1",
            playback_id="pending-accepted-1",
            locator=locator,
            title="Confirmed title",
            subtitle="Confirmed subtitle",
        )

    assert len(factory.calls) == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_launch_samples_progress_periodically_and_completion_carries_final_sample(
    tmp_path: Path,
) -> None:
    process = FakePlayerProcess(31102)
    ipc = RecordingIpc(property_values=[(120.0, 1800.0), (1785.0, 1800.0)])
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[],
        processes=[process],
        ipc=ipc,
        progress_interval_seconds=0.001,
    )
    progress: list[PlaybackProgress] = []
    completions: list[PlaybackCompletion] = []

    async def on_progress(sample: PlaybackProgress) -> None:
        progress.append(sample)
        manager._progress_interval_seconds = 60.0

    async def on_complete(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_progress_handler(on_progress)
    manager.bind_completion_handler(on_complete)
    await manager.launch(
        session_id="voice-session-1",
        playback_id="pending-anime-01",
        locator=DirectStreamLocator(
            stream_url="https://hls.anidb.app/stream/example/ep.m3u8"
        ),
        title="Cowboy Bebop",
        subtitle="Episode 1 · Subbed",
    )
    for _ in range(20):
        if progress:
            break
        await asyncio.sleep(0.001)
    process.exit(0)
    for _ in range(20):
        if completions:
            break
        await asyncio.sleep(0.001)

    assert progress[0] == PlaybackProgress(
        session_id="voice-session-1",
        playback_id="pending-anime-01",
        position_seconds=120.0,
        duration_seconds=1800.0,
    )
    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id="pending-anime-01",
            title="Cowboy Bebop",
            channel="Episode 1 · Subbed",
            outcome="completed",
            position_seconds=1785.0,
            duration_seconds=1800.0,
        )
    ]


@pytest.mark.asyncio
async def test_progress_handler_failure_never_blocks_player_cleanup(tmp_path: Path) -> None:
    process = FakePlayerProcess(31103)
    ipc = RecordingIpc(property_values=[(300.0, 1800.0)])
    manager, _resolver, _factory, _ipc, _signaler = _manager(
        tmp_path,
        videos=[],
        processes=[process],
        ipc=ipc,
        progress_interval_seconds=0.001,
    )

    async def failing_progress(_sample: PlaybackProgress) -> None:
        raise OSError("disk full")

    manager.bind_progress_handler(failing_progress)
    await manager.launch(
        session_id="voice-session-1",
        playback_id="pending-anime-01",
        locator=DirectStreamLocator(
            stream_url="https://hls.anidb.app/stream/example/ep.m3u8"
        ),
        title="Cowboy Bebop",
        subtitle="Episode 1 · Subbed",
    )
    await asyncio.sleep(0.01)
    await asyncio.wait_for(manager.shutdown(), timeout=0.2)

    assert manager.status == {"status": "idle"}


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
        "position_seconds",
        "duration_seconds",
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
    await _launch_confirmed(manager)

    process.exit(0)
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    await asyncio.wait_for(released.wait(), timeout=0.2)

    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id="playback-1",
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
    await _launch_confirmed(manager)

    process.exit(1)
    for _ in range(20):
        if completions:
            break
        await asyncio.sleep(0)

    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id="playback-1",
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
    await _launch_confirmed(manager)

    if action == "shutdown":
        await manager.shutdown()
    else:
        await manager.stop_for_session("voice-session-1", "playback-1")

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
    await _launch_confirmed(manager)

    await manager.stop_for_session("stale-session", "playback-1")
    await manager.stop_for_session("voice-session-1", "older-playback")

    assert completions == []
    assert manager.status["status"] == "playing"
    await manager.shutdown()




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

    with caplog.at_level(logging.ERROR, logger="realtime_action_spike.media"):
        await _launch_confirmed(manager)
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
        await _launch_confirmed(manager)

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
    await _launch_confirmed(manager)

    await manager.shutdown()
    await manager.shutdown()

    assert [pid for pid, _signal in signaler.calls] == [process.pid]
    assert manager.status == {"status": "idle"}


@pytest.mark.asyncio
async def test_real_subprocess_ipc_prepare_commit_and_process_group_shutdown(
    short_runtime_dir: Path,
) -> None:
    fake_mpv = short_runtime_dir / "fake-mpv"
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
        runtime_directory=short_runtime_dir / "runtime" / "media",
        ipc_timeout_seconds=1.0,
        process_exit_timeout_seconds=1.0,
    )

    await _launch_confirmed(
        manager,
        playback_id="playback-real",
        session_id="voice-session-real",
    )

    assert manager.status["status"] == "playing"
    await manager.shutdown()
    assert manager.status == {"status": "idle"}
    assert list((short_runtime_dir / "runtime").glob("media/*.sock")) == []
