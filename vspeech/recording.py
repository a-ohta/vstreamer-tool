import audioop
from asyncio import AbstractEventLoop
from asyncio import CancelledError
from asyncio import Queue
from asyncio import to_thread
from math import log
from typing import Any
from typing import Callable
from typing import Optional
from typing import Tuple

from pyaudio import PyAudio
from pyaudio import Stream

from vspeech.audio import get_device_name
from vspeech.audio import search_device
from vspeech.config import RecordingConfig
from vspeech.logger import logger
from vspeech.shared_context import EventType
from vspeech.shared_context import SharedContext
from vspeech.shared_context import SoundOutput
from vspeech.shared_context import WorkerOutput


def open_input_stream(
    audio: PyAudio,
    config: RecordingConfig,
    stream_callback: Optional[
        Callable[[Optional[bytes], int, Any, int], Tuple[Optional[bytes], int]]
    ] = None,
):
    input_device_index = config.input_device_index
    if input_device_index is None:
        input_device = search_device(
            audio,
            host_api_type=config.input_host_api_name,
            name=config.input_device_name,
            input=True,
        )
        if not input_device:
            raise TypeError("not found input device")
        input_device_index = input_device.index
    input_device_name = get_device_name(audio, input_device_index)
    logger.info("use input device %s: %s", input_device_index, input_device_name)
    return audio.open(
        input_device_index=input_device_index,
        format=config.format,
        channels=config.channels,
        rate=config.rate,
        input=True,
        output=False,
        frames_per_buffer=config.chunk,
        stream_callback=stream_callback,
    )


def get_dbfs(interval_frames: bytes, sample_width: int):
    rms = audioop.rms(interval_frames, sample_width)
    max_possible_val = (2 ** (sample_width * 8)) / 2
    if rms == 0:
        return float("-inf")
    return 20 * log(rms / max_possible_val, 10)


async def recording_task_loop(
    stream: Stream, config: RecordingConfig, sample_width: int
):
    interval_frame_count = 0
    interval_frames: bytes = b""
    speaking_frames: bytes = b""
    last_interval_frames: bytes = b""
    total_speaking_seconds = 0
    status = "waiting"
    while stream.is_active():
        in_data = await to_thread(stream.read, config.chunk)
        interval_frame_count += config.chunk
        interval_frames += in_data
        approx_max_amp = get_dbfs(interval_frames, sample_width=sample_width)
        if interval_frame_count >= config.rate * config.record_interval_sec:
            speaking = approx_max_amp > config.silence_threshold
            if status == "waiting" and speaking:
                logger.info("voice recording...")
                speaking_frames += last_interval_frames + interval_frames
                status = "speaking"
            elif status == "speaking":
                speaking_frames += interval_frames
                total_speaking_seconds += config.record_interval_sec
                if not speaking or config.max_recording_sec < total_speaking_seconds:
                    logger.info("voice stopped")
                    status = "stopped"
            elif status == "stopped":
                speaking_frames += interval_frames
                total_speaking_seconds += config.record_interval_sec
                if not speaking or config.max_recording_sec < total_speaking_seconds:
                    logger.info("voice recorded")
                    yield speaking_frames
                    status = "waiting"
                    speaking_frames = b""
                    interval_frames = b""
                elif speaking:
                    status = "speaking"
            last_interval_frames = interval_frames
            interval_frame_count = 0
            interval_frames = b""


async def recording_worker(context: SharedContext, out_queue: Queue[WorkerOutput]):
    while True:
        stream = open_input_stream(context.audio, context.config.recording)
        sample_width = context.audio.get_sample_size(context.config.recording.format)
        try:
            async for frames in recording_task_loop(
                stream=stream,
                config=context.config.recording,
                sample_width=sample_width,
            ):
                if not context.resume.is_set():
                    logger.info("recording have been paused")
                    break
                out_queue.put_nowait(
                    WorkerOutput(
                        source=EventType.recording,
                        sound=SoundOutput(
                            data=frames,
                            rate=context.config.recording.rate,
                            format=context.config.recording.format,
                            channels=context.config.recording.channels,
                        ),
                        text=None,
                    )
                )
        except CancelledError:
            logger.debug("recording worker cancelled")
            stream.close()
            raise
        stream.close()
        try:
            await context.resume.wait()
        except CancelledError:
            logger.debug("recording worker cancelled")
            raise


def create_recording_task(loop: AbstractEventLoop, context: SharedContext):
    return loop.create_task(
        recording_worker(context, out_queue=context.sender_queue), name="recording_task"
    )
