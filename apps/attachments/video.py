"""Video derivatives via ffmpeg/ffprobe: poster frames and web-playable copies.

Two problems, one toolchain. A gallery of videos renders as a wall of
black rectangles until the browser has range-requested the moov atom of
every file on the page (up to 50 MB each, MAX_FILE_SIZE), and a phone
``.mov`` is served as ``video/quicktime``, which Chrome and Firefox
refuse to play at all no matter how ordinary the H.264 inside it is. So
a video attachment gets a poster frame, and - when the original is not
universally playable - an H.264/AAC MP4 *beside* it.

INV-14 governs everything here: derived media is never authoritative.
The uploaded original is never modified, renamed or deleted. A damage
video is the evidentiary record of asset condition (INV-08, INV-12), so
the transcode is an additional file and the gallery always keeps a route
back to the untouched bytes. Nothing in this module writes to, moves or
removes its source.

The module is deliberately Django-model-free and Celery-free: pure
functions over filesystem paths, so the skip rule and every argv list
are unit-testable with literal dicts and zero subprocess calls.
``apps.attachments.services`` owns the model round-trip and decides
*where* these run - poster extraction (~0.3 s) may follow the existing
inline-in-eager-mode thumbnail pattern, transcoding (minutes) never runs
on a request path.

ffmpeg is optional. CI has none, and an image built before this feature
has none either, so an empty or unresolvable binary setting is a clean,
logged no-op on every path rather than an exception - exactly like
``imaging.build_thumbnail_bytes`` returning ``None`` for bytes Pillow
cannot decode. Every entry point returns a value, never raises, and
leaves the attachment serving its original.

Subprocess rules, each of which has already bitten this feature once:
argv lists only (never a command string handed to a command interpreter),
always ``-nostdin`` and ``-y`` so ffmpeg can never block on a terminal it
does not have, and always an explicit ``timeout=`` so a pathological file
cannot pin a worker slot forever.
"""

import io
import json
import logging
import os
import shutil
import subprocess

from django.conf import settings

from .imaging import build_thumbnail_bytes

logger = logging.getLogger(__name__)

#: Long edge of the stored poster, in pixels. Videos render in
#: ``col-12 col-md-6 col-lg-4`` tiles (~350-425 CSS px), roughly three
#: times wider than a photo thumbnail's tile, so the poster does not
#: inherit ``imaging.THUMBNAIL_MAX_EDGE`` (300 px would visibly upscale).
#: Measured through the real pipeline: 1080p -> 640x360, ~15 KB.
POSTER_MAX_EDGE = 640
#: Input-seek offset. One second in, not zero, because the first frame of
#: phone footage is routinely black or mid-auto-exposure.
POSTER_SEEK_SECONDS = "1"
#: Retry offset when the seek landed past the end of a very short clip -
#: ffmpeg exits 0 and writes nothing in that case, so the retry is driven
#: by empty output, never by the return code.
POSTER_SEEK_RETRY = "0"
#: ``-q:v`` for the intermediate full-size frame. Generous on purpose: it
#: is thrown away immediately after Pillow downscales it, and artefacts
#: introduced here would be baked into the stored poster.
FRAME_QUALITY = "2"
#: Long-edge cap applied on the ENCODE branch only - a stream copy cannot
#: scale, and re-encoding a compliant file purely to shrink it would
#: trade evidentiary fidelity for bytes the browser never fetches now
#: that a poster lets the tag drop to preload="none".
TRANSCODE_MAX_EDGE = 1280
TRANSCODE_CRF = "26"
#: Not the roadmap's "medium": this is a web proxy of an archived master,
#: and medium roughly doubles wall time for ~10 % size.
TRANSCODE_PRESET = "veryfast"
TRANSCODE_AUDIO_BITRATE = "128k"

#: The container is judged by the *stored extension*, never by ffprobe's
#: ``format.format_name`` - which is the identical string
#: "mov,mp4,m4a,3gp,3g2,mj2" for a .mov and a .mp4. The extension is also
#: what ``views.protected_media`` turns into the served Content-Type, and
#: that Content-Type is what the browser judges, so it is the only thing
#: that can decide playability here.
BROWSER_CONTAINERS = frozenset({"mp4"})
BROWSER_VIDEO_CODECS = frozenset({"h264"})
#: ``codec_name == "h264"`` is not sufficient: High 10 and 4:2:2 profiles
#: (yuv420p10le, yuv422p) probe as h264 and decode in no mainstream
#: browser. yuvj420p is allowed because full-range 4:2:0 is ordinary
#: phone output and plays everywhere.
BROWSER_PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})
#: About browser *decoding*, not mux success: ffmpeg will happily mux
#: pcm_s16le into an MP4, and no browser will play the result.
BROWSER_AUDIO_CODECS = frozenset({"aac", "mp3"})

#: The quotes are literal characters ffmpeg's own filtergraph parser
#: consumes, NOT shell quoting - they survive into the argv list on
#: purpose. Without them the comma inside ``min()`` is read as a filter
#: separator and ffmpeg dies with "No option name near 'min(1280'".
SCALE_FILTER = (
    "scale=w='min({edge},iw)':h='min({edge},ih)'"
    ":force_original_aspect_ratio=decrease:force_divisible_by=2"
)

#: ffmpeg's stderr on a corrupt file runs to megabytes; only the tail is
#: ever diagnostic. The transcode gets a longer tail because its failures
#: are worth the context, a failed poster is nearly always a one-liner.
POSTER_STDERR_TAIL = 500
TRANSCODE_STDERR_TAIL = 2000

#: Defaults mirrored from the ``VIDEO_*`` block in config/settings/base.py
#: so this module stays importable (and a clean no-op) under any settings
#: module that predates it.
DEFAULT_PROBE_TIMEOUT = 15
DEFAULT_POSTER_TIMEOUT = 30
DEFAULT_TRANSCODE_TIMEOUT = 900
DEFAULT_TRANSCODE_THREADS = 2

#: Log-once guard, keyed by setting name. A module-level set rather than
#: an ``lru_cache`` on the lookup itself: caching would freeze binary
#: availability for the process lifetime and silently poison any test
#: that flips ``VIDEO_FFMPEG_BIN`` via ``override_settings``. Tests clear
#: this in ``setUp``.
_warned: set[str] = set()


def _int_setting(name: str, default: int) -> int:
    """Read an integer ``VIDEO_*`` setting, falling back to its default."""
    return int(getattr(settings, name, default))


def _warn_once(key: str, message: str, *args) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen.

    A missing binary is a permanent, whole-process condition: without
    this, a backfill run over a thousand videos would emit a thousand
    identical warnings and bury everything else in the log.
    """
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def _tail(stderr: bytes | None, limit: int) -> str:
    """Decode and truncate a subprocess's stderr for logging."""
    if not stderr:
        return ""
    return stderr.decode("utf-8", "replace")[-limit:]


def binary(setting_name: str) -> str | None:
    """Resolve a configured ffmpeg-family binary to a runnable path.

    Returns ``None`` - meaning "this feature is off right now" - when the
    setting is empty (the deliberate kill switch, and what the test
    settings pin so the suite can never acquire a hidden ffmpeg
    dependency) or when the configured name is not on PATH (CI, and any
    image built before ffmpeg was added to the Dockerfile).

    Not cached: ``shutil.which`` is a ~1 ms PATH scan running at most
    twice per attachment, and it also validates an absolute path, so
    ``VIDEO_FFMPEG_BIN=C:\\ffmpeg\\bin\\ffmpeg.exe`` works on the dev box.
    The noise a cache would suppress is handled by ``_warn_once``
    instead, which tests can clear.
    """
    configured = (getattr(settings, setting_name, "") or "").strip()
    if not configured:
        # Empty is a choice, not a failure: DEBUG, not WARNING.
        logger.debug("%s is empty; video derivation is switched off.", setting_name)
        return None
    resolved = shutil.which(configured)
    if resolved is None:
        _warn_once(
            setting_name,
            "%s=%r is not on PATH; video derivatives are skipped. Install ffmpeg "
            "(it is in the Docker image) and run `manage.py "
            "backfill_video_derivatives` to repair the backlog.",
            setting_name,
            configured,
        )
        return None
    return resolved


def _run(argv: list[str], *, timeout: int, capture_stdout: bool, warn_key: str):
    """Run an ffmpeg-family argv list, or return ``None`` if it could not run.

    ``None`` covers every way the process failed to produce a verdict
    (binary vanished between ``shutil.which`` and here, timed out, OS
    refused the spawn); a returned ``CompletedProcess`` may still carry a
    non-zero return code, which callers judge themselves.

    ``check=False`` on purpose: this module never raises at its callers.
    ``stdin`` is closed rather than inherited so ffmpeg cannot block
    waiting for input in a worker with no terminal, on top of
    ``-nostdin``.
    """
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        _warn_once(warn_key, "%s is no longer executable; skipping.", argv[0])
        return None
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child by the time this
        # is raised, so there is nothing to clean up here.
        logger.warning(
            "%s timed out after %ss on %s; giving up.", argv[0], timeout, argv[-1]
        )
        return None
    except OSError:  # pragma: no cover - defensive; a spawn must never propagate
        logger.exception("Could not start %s.", argv[0])
        return None


def probe(path: str) -> dict | None:
    """Return ffprobe's JSON description of ``path``, or ``None``.

    ``None`` means "nothing readable here" and is returned for every
    failure mode uniformly: ffprobe missing, non-zero exit, empty output,
    or output that is not a JSON object. Ten bytes of junk named
    ``.mp4`` exits 1 with ``{}`` on stdout and "moov atom not found" on
    stderr - it must parse to ``None``, never raise.
    """
    ffprobe = binary("VIDEO_FFPROBE_BIN")
    if ffprobe is None:
        return None
    argv = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = _run(
        argv,
        timeout=_int_setting("VIDEO_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT),
        capture_stdout=True,
        warn_key="VIDEO_FFPROBE_BIN",
    )
    if result is None:
        return None
    if result.returncode != 0 or not result.stdout:
        logger.info(
            "ffprobe could not read %s (rc=%s): %s",
            path,
            result.returncode,
            _tail(result.stderr, POSTER_STDERR_TAIL),
        )
        return None
    try:
        info = json.loads(result.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeError):
        logger.info("ffprobe returned unparseable JSON for %s.", path)
        return None
    return info if isinstance(info, dict) else None


def video_stream(info: dict | None) -> dict | None:
    """First real video stream in an ffprobe result, or ``None``.

    Cover art embedded in an MP4 appears as a video stream with
    ``disposition.attached_pic = 1``; treating it as the picture would
    make the skip rule judge an album thumbnail instead of the footage,
    so it is passed over here (and excluded again by ``0:V:0`` in the
    argv lists).
    """
    for stream in (info or {}).get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream
    return None


def audio_stream(info: dict | None) -> dict | None:
    """First audio stream in an ffprobe result, or ``None`` if silent."""
    for stream in (info or {}).get("streams") or []:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def is_browser_ready(stored_name: str, info: dict | None) -> bool:
    """Is this file playable in every browser as-is - i.e. leave it alone?

    The skip rule, kept a pure function so its whole truth table is
    testable with literal dicts and no subprocess. ``True`` means no
    second copy is written at all.

    All three clauses must hold:

    1. The **stored extension** is ``.mp4``. Not ffprobe's
       ``format.format_name``, which is byte-identical for a .mov and a
       .mp4 - a rule keyed on it would silently never transcode the
       exact files this feature exists for, and would do so with green
       tests. This clause is the whole reason the feature exists: a .mov
       is served as video/quicktime, which Chrome and Firefox refuse
       regardless of the H.264 inside.
    2. There is a real video stream, in an allowlisted codec *and* pixel
       format (see ``BROWSER_PIXEL_FORMATS``).
    3. There is no audio, or it is in an allowlisted codec.

    Deliberately NOT part of the rule:

    * **Resolution and bitrate.** Re-encoding a compliant 4K .mp4 purely
      to reclaim bandwidth trades evidentiary fidelity and CPU for a file
      the browser now does not fetch until the user presses play, and
      uploads are already bounded by MAX_FILE_SIZE.
    * **faststart / moov position.** ffprobe cannot report it cheaply,
      and with MEDIA_ACCEL_REDIRECT nginx serves byte ranges, so a
      trailing moov costs one extra range request. Everything this
      module *produces* gets ``-movflags +faststart`` anyway.
    * **Output size.** See ``transcode_to_mp4``.
    """
    extension = stored_name.rsplit(".", 1)[-1].lower() if "." in stored_name else ""
    if extension not in BROWSER_CONTAINERS:
        return False
    stream = video_stream(info)
    if stream is None:
        return False
    if stream.get("codec_name") not in BROWSER_VIDEO_CODECS:
        return False
    if stream.get("pix_fmt") not in BROWSER_PIXEL_FORMATS:
        return False
    audio = audio_stream(info)
    if audio is not None and audio.get("codec_name") not in BROWSER_AUDIO_CODECS:
        return False
    return True


def build_poster_argv(ffmpeg: str, src: str, seek: str) -> list[str]:
    """argv for extracting one full-size JPEG frame onto stdout.

    Every flag here is load-bearing:

    * ``-ss`` **before** ``-i`` is input seeking - keyframe-accurate and
      decoding roughly one frame, instead of decoding everything up to
      the offset. It also steps past the black opening frame.
    * ``0:V:0`` - capital V - selects the first real video stream,
      excluding ``attached_pic`` cover art.
    * ``-an -sn -dn`` drops the audio, subtitle and timecode (``tmcd``)
      tracks iPhones attach; without them the image2 muxer complains.
    * ``-pix_fmt yuvj420p`` keeps the mjpeg encoder from refusing to
      initialise on full-range sources ("Non full-range YUV is
      non-standard").
    * ``pipe:1`` - the frame comes back in memory, bounded by
      ``-frames:v 1`` (~114 KB for 1080p), so there is no temp file and
      therefore no cleanup path to get wrong.

    Note the absence of ``-vf``: Pillow does the downscaling, so sizing
    and JPEG quality stay in ``imaging`` alone, "never upscale" is a
    property of ``Image.thumbnail`` rather than a hand-written filter
    expression, and posters come out matching photo thumbnails.
    """
    return [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        seek,
        "-i",
        src,
        "-map",
        "0:V:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "-q:v",
        FRAME_QUALITY,
        "-pix_fmt",
        "yuvj420p",
        "pipe:1",
    ]


def build_poster_bytes(src_path: str) -> bytes | None:
    """Return downscaled JPEG poster bytes for a video file, or ``None``.

    ``None`` is the uniform "no poster" answer - ffmpeg absent, the file
    undecodable, the frame unreadable by Pillow - and the caller simply
    leaves ``Attachment.thumbnail`` empty, exactly as a photo whose bytes
    Pillow cannot decode does today.

    **Success is judged on the output, not on the return code.** Seeking
    a second into a 0.4 s clip exits 0 and writes nothing at all, so an
    empty stdout triggers exactly one retry from the very start of the
    file - which also rescues files whose duration metadata lies.
    """
    ffmpeg = binary("VIDEO_FFMPEG_BIN")
    if ffmpeg is None:
        return None
    timeout = _int_setting("VIDEO_POSTER_TIMEOUT", DEFAULT_POSTER_TIMEOUT)
    result = None
    for seek in (POSTER_SEEK_SECONDS, POSTER_SEEK_RETRY):
        result = _run(
            build_poster_argv(ffmpeg, src_path, seek),
            timeout=timeout,
            capture_stdout=True,
            warn_key="VIDEO_FFMPEG_BIN",
        )
        if result is None:
            return None
        if result.stdout:
            return _poster_from_frame(result.stdout, src_path)
        logger.debug("No frame at -ss %s for %s; retrying from the start.", seek, src_path)
    logger.info(
        "ffmpeg produced no poster frame for %s (rc=%s): %s",
        src_path,
        result.returncode,
        _tail(result.stderr, POSTER_STDERR_TAIL),
    )
    return None


def _poster_from_frame(frame: bytes, src_path: str) -> bytes | None:
    """Downscale one extracted frame, discarding a pathological result.

    A poster at least as large as the video it stands in for is a sign
    something went wrong (a one-frame "video", a decode that produced
    noise); storing it would cost more than it saves, and the gallery
    degrades gracefully to a poster-less tile.
    """
    poster = build_thumbnail_bytes(io.BytesIO(frame), max_edge=POSTER_MAX_EDGE)
    if poster is None:
        return None
    try:
        if len(poster) >= os.path.getsize(src_path):
            logger.info("Poster for %s is not smaller than the video; discarding.", src_path)
            return None
    except OSError:  # pragma: no cover - the source was readable moments ago
        pass
    return poster


def build_transcode_argv(
    ffmpeg: str,
    src: str,
    dst: str,
    *,
    copy_video: bool,
    copy_audio: bool,
    threads: int,
) -> list[str]:
    """argv for producing the browser-playable MP4 beside the original.

    Video and audio are two independent decisions, not three tiers: an
    iPhone ``.mov`` in H.264/AAC - the most common real input - is
    therefore a pure container remux (measured 0.089 s for 2.36 MB) with
    zero generational loss on evidence, while a PCM soundtrack costs an
    audio re-encode only.

    * ``-map 0:a:0?`` - the trailing ``?`` makes audio optional, so a
      silent clip transcodes cleanly instead of failing the whole job.
    * ``-pix_fmt yuv420p`` on the encode branch is required, not
      cosmetic: iPhone HEVC arrives as yuv420p10le, which decodes
      nowhere useful.
    * Scaling appears only on the encode branch - a stream copy cannot
      scale, and when a full re-encode is happening anyway, capping the
      long edge is free.
    * ``-movflags +faststart`` moves the moov atom to the front so
      playback can start before the file is fully fetched.
    """
    argv = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        src,
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
    ]
    if copy_video:
        argv += ["-c:v", "copy"]
    else:
        argv += [
            "-c:v",
            "libx264",
            "-preset",
            TRANSCODE_PRESET,
            "-crf",
            TRANSCODE_CRF,
            "-pix_fmt",
            "yuv420p",
            "-vf",
            SCALE_FILTER.format(edge=TRANSCODE_MAX_EDGE),
            "-threads",
            str(threads),
        ]
    if copy_audio:
        argv += ["-c:a", "copy"]
    else:
        argv += ["-c:a", "aac", "-b:a", TRANSCODE_AUDIO_BITRATE, "-ac", "2"]
    argv += [
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "1024",
        "-f",
        "mp4",
        dst,
    ]
    return argv


def transcode_to_mp4(
    src_path: str,
    dest_path: str,
    info: dict,
    *,
    threads: int | None = None,
) -> bool:
    """Write a browser-playable MP4 of ``src_path`` to ``dest_path``.

    ``info`` is a previous ``probe()`` result and decides, per stream,
    whether this is a remux or a re-encode. ``dest_path`` must live in a
    caller-owned ``tempfile.TemporaryDirectory`` - never a
    ``NamedTemporaryFile``, whose open handle blocks ffmpeg on Windows -
    so the context manager is also the only cleanup path and it runs on
    timeouts and on the row-deleted-mid-encode path alike.

    Returns ``True`` only when ffmpeg exited 0 **and** actually wrote
    bytes; a zero return code alone is not proof of output. The caller
    keeps the result even when it is larger than the original: this is a
    deliberate departure from the discard-if-larger rule photos follow
    (POR-05), because the skip rule means every file that reaches here
    was *not reliably playable* - discarding a larger output would
    restore exactly the bug this feature fixes.

    The source is only ever read. It is never moved, rewritten or
    deleted (INV-14).
    """
    ffmpeg = binary("VIDEO_FFMPEG_BIN")
    if ffmpeg is None:
        return False
    if threads is None:
        threads = _int_setting("VIDEO_TRANSCODE_THREADS", DEFAULT_TRANSCODE_THREADS)
    stream = video_stream(info)
    audio = audio_stream(info)
    copy_video = (
        stream is not None
        and stream.get("codec_name") in BROWSER_VIDEO_CODECS
        and stream.get("pix_fmt") in BROWSER_PIXEL_FORMATS
    )
    copy_audio = audio is None or audio.get("codec_name") in BROWSER_AUDIO_CODECS
    argv = build_transcode_argv(
        ffmpeg,
        src_path,
        dest_path,
        copy_video=copy_video,
        copy_audio=copy_audio,
        threads=threads,
    )
    result = _run(
        argv,
        timeout=_int_setting("VIDEO_TRANSCODE_TIMEOUT", DEFAULT_TRANSCODE_TIMEOUT),
        capture_stdout=False,
        warn_key="VIDEO_FFMPEG_BIN",
    )
    if result is None:
        return False
    # Never test for a specific code: Windows reported -22 as 4294967274.
    if result.returncode != 0:
        logger.warning(
            "ffmpeg could not transcode %s (rc=%s): %s",
            src_path,
            result.returncode,
            _tail(result.stderr, TRANSCODE_STDERR_TAIL),
        )
        return False
    try:
        produced = os.path.getsize(dest_path)
    except OSError:
        produced = 0
    if produced <= 0:
        logger.warning(
            "ffmpeg exited 0 but wrote no bytes for %s; treating as a failure.", src_path
        )
        return False
    return True
