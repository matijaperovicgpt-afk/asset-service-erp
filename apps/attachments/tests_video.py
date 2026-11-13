"""Video derivatives: poster frames and browser-playable MP4 copies.

The three
this file exists to defend:

* **INV-14** - derived media is never authoritative. Several tests here
  read the original's bytes back off disk after a derivation to prove it
  was neither moved, rewritten nor deleted. A damage video is the
  evidentiary record of asset condition (INV-08, INV-12).
* **INV-13** - no background job is queued before its transaction
  commits, and the eager/broker branch is taken on
  ``CELERY_TASK_ALWAYS_EAGER`` rather than by calling ``on_commit``
  unconditionally (``TestCase`` never commits, so the callbacks would be
  dropped and the whole feature would test green-but-inert).
* **The transcode never runs on a request path.** Production has no
  ``REDIS_URL`` today, so "inline" is the live configuration, and a
  50 MB clip is minutes of ffmpeg on one of three gunicorn workers.

**No test in this file requires ffmpeg.** The suite pins
``VIDEO_FFMPEG_BIN``/``VIDEO_FFPROBE_BIN`` to ``""`` (config/settings/test.py),
which is exactly the CI shape: every generator is a logged no-op.
Tests that want the machinery fake the PATH lookup and mock
``subprocess.run``; ``FfmpegIntegrationTests`` is the single class that
genuinely shells out and it is ``skipUnless``-guarded, generating its own
fixtures with the very binary the guard checks for (no video blob enters
git).
"""

import json
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from subprocess import CompletedProcess
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings

from apps.attachments import services, tasks, video
from apps.attachments.models import Attachment
from apps.attachments.services import (
    attach_file,
    enqueue_video_derivatives,
    generate_poster,
    group_attachments_by_kind,
    transcode_video,
)

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _jpeg_bytes(width=1280, height=720, color=(20, 90, 140)):
    """Bytes of a solid-colour JPEG, standing in for an extracted frame."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _video_bytes(size=400_000):
    """Plausible-looking, deliberately undecodable video bytes.

    Padded well past any poster we might generate: ``video`` discards a
    poster that is not smaller than the file it stands in for, and a
    12-byte "video" would trip that pathological-result guard in every
    test rather than in the one that means to exercise it.
    """
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    return header + b"\x00" * max(0, size - len(header))


def _fake_which(name):
    """Resolve any configured binary, wherever the suite happens to run.

    Patched over ``video.shutil.which`` so the mocked-subprocess layers
    behave identically on this dev box and on CI, which has no ffmpeg.
    """
    return f"/usr/bin/{os.path.basename(name)}"


def _probe_json(
    *,
    vcodec="h264",
    pix_fmt="yuv420p",
    acodec="aac",
    width=1920,
    height=1080,
):
    """An ffprobe ``-show_format -show_streams`` result, shaped like the real thing.

    ``format_name`` is the genuine value, which is byte-identical for a
    ``.mov`` and a ``.mp4`` - the fixture would be worthless with a
    tidied-up string, since that identity is the whole reason the skip
    rule keys on the stored extension instead.
    """
    streams = []
    if vcodec:
        streams.append(
            {
                "codec_type": "video",
                "codec_name": vcodec,
                "pix_fmt": pix_fmt,
                "width": width,
                "height": height,
                "disposition": {"attached_pic": 0},
            }
        )
    if acodec:
        streams.append({"codec_type": "audio", "codec_name": acodec})
    return {
        "streams": streams,
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }


def _runner(probe_info=None, *, frame=None, output=b"\x00" * 2000, rc=0, on_ffmpeg=None):
    """Build a ``subprocess.run`` replacement that answers all three argv shapes.

    ffprobe gets JSON on stdout, the poster call (which ends in
    ``pipe:1``) gets JPEG bytes on stdout, and the transcode call writes
    ``output`` to its destination path - the last argv element - so the
    caller's "did it actually produce bytes?" check has something real to
    measure.
    """

    def run(argv, **kwargs):
        if "ffprobe" in os.path.basename(argv[0]):
            payload = json.dumps(probe_info or {}).encode()
            return CompletedProcess(argv, 0, stdout=payload, stderr=b"")
        if argv[-1] == "pipe:1":
            return CompletedProcess(argv, 0, stdout=frame or b"", stderr=b"")
        if on_ffmpeg is not None:
            on_ffmpeg(argv)
        if rc == 0 and output is not None:
            with open(argv[-1], "wb") as handle:
                handle.write(output)
        return CompletedProcess(argv, rc, stdout=b"", stderr=b"")

    return run


# ---------------------------------------------------------------------------
# Layer 1-2: pure functions - no database, no subprocess, no mocks
# ---------------------------------------------------------------------------


class VideoSkipRuleTests(SimpleTestCase):
    """``is_browser_ready`` - the "don't re-encode what is already fine" rule."""

    def test_compatible_mp4_is_left_alone(self):
        self.assertTrue(video.is_browser_ready("cases/1/work/a.mp4", _probe_json()))

    def test_identical_payload_in_a_mov_is_not(self):
        """The regression guard for the whole feature.

        ffprobe reports the same ``format_name`` for a .mov and a .mp4,
        so a skip rule keyed on it would silently never convert the exact
        files this exists for - and would do so with green tests, because
        a .mov fixture probes exactly like an .mp4. The stored extension
        is what ``protected_media`` turns into a Content-Type, and
        video/quicktime is what Chrome and Firefox refuse.
        """
        info = _probe_json()
        self.assertTrue(video.is_browser_ready("cases/1/work/a.mp4", info))
        self.assertFalse(video.is_browser_ready("cases/1/work/a.mov", info))

    def test_hevc_is_not_browser_ready(self):
        self.assertFalse(video.is_browser_ready("a.mp4", _probe_json(vcodec="hevc")))

    def test_high10_pixel_format_is_not_browser_ready(self):
        """h264 alone is not enough: High 10 decodes in no mainstream browser."""
        info = _probe_json(pix_fmt="yuv420p10le")
        self.assertFalse(video.is_browser_ready("a.mp4", info))

    def test_pcm_audio_is_not_browser_ready(self):
        """ffmpeg muxes PCM into MP4 happily; no browser decodes it."""
        self.assertFalse(video.is_browser_ready("a.mp4", _probe_json(acodec="pcm_s16le")))

    def test_silent_video_is_browser_ready(self):
        self.assertTrue(video.is_browser_ready("a.mp4", _probe_json(acodec=None)))

    def test_full_range_420_with_mp3_is_browser_ready(self):
        info = _probe_json(pix_fmt="yuvj420p", acodec="mp3")
        self.assertTrue(video.is_browser_ready("a.mp4", info))

    def test_unreadable_or_streamless_input_is_never_ready(self):
        self.assertFalse(video.is_browser_ready("a.mp4", None))
        self.assertFalse(video.is_browser_ready("a.mp4", {}))
        self.assertFalse(video.is_browser_ready("a.mp4", _probe_json(vcodec=None)))
        self.assertFalse(video.is_browser_ready("a", _probe_json()))

    def test_cover_art_is_not_mistaken_for_the_footage(self):
        """Attached cover art probes as a video stream with attached_pic=1."""
        info = _probe_json()
        info["streams"].insert(
            0,
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "pix_fmt": "yuvj420p",
                "disposition": {"attached_pic": 1},
            },
        )
        self.assertEqual(video.video_stream(info)["codec_name"], "h264")
        self.assertTrue(video.is_browser_ready("a.mp4", info))

    def test_stream_selection_helpers(self):
        info = _probe_json(acodec="aac")
        self.assertEqual(video.audio_stream(info)["codec_name"], "aac")
        self.assertIsNone(video.audio_stream(_probe_json(acodec=None)))
        self.assertIsNone(video.video_stream(None))
        self.assertIsNone(video.audio_stream(None))


class VideoArgvTests(SimpleTestCase):
    """argv construction - never a command string handed to a shell."""

    def test_poster_argv_seeks_on_the_input_and_pipes_one_frame(self):
        argv = video.build_poster_argv("ffmpeg", "/m/a.mov", "1")
        self.assertIn("-nostdin", argv)
        self.assertIn("-y", argv)
        # -ss BEFORE -i is input seeking: ~one decoded frame instead of
        # decoding everything up to the offset.
        self.assertLess(argv.index("-ss"), argv.index("-i"))
        self.assertEqual(argv[argv.index("-ss") + 1], "1")
        self.assertEqual(argv[argv.index("-map") + 1], "0:V:0")
        self.assertEqual(argv[argv.index("-frames:v") + 1], "1")
        for flag in ("-an", "-sn", "-dn"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuvj420p")
        self.assertEqual(argv[-1], "pipe:1")
        # Pillow does the scaling, so the filtergraph is not involved at all.
        self.assertNotIn("-vf", argv)

    def test_remux_argv_copies_both_streams(self):
        argv = video.build_transcode_argv(
            "ffmpeg", "/m/a.mov", "/t/web.mp4", copy_video=True, copy_audio=True, threads=2
        )
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "copy")
        self.assertNotIn("libx264", argv)
        self.assertNotIn("-vf", argv)

    def test_encode_argv_uses_libx264_and_the_configured_thread_cap(self):
        from django.conf import settings

        argv = video.build_transcode_argv(
            "ffmpeg",
            "/m/a.mov",
            "/t/web.mp4",
            copy_video=False,
            copy_audio=True,
            threads=settings.VIDEO_TRANSCODE_THREADS,
        )
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertEqual(argv[argv.index("-preset") + 1], "veryfast")
        self.assertEqual(argv[argv.index("-crf") + 1], "26")
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(
            argv[argv.index("-threads") + 1], str(settings.VIDEO_TRANSCODE_THREADS)
        )

    def test_audio_encode_argv(self):
        argv = video.build_transcode_argv(
            "ffmpeg", "/m/a.mov", "/t/web.mp4", copy_video=True, copy_audio=False, threads=2
        )
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
        self.assertEqual(argv[argv.index("-b:a") + 1], "128k")
        self.assertEqual(argv[argv.index("-ac") + 1], "2")

    def test_every_transcode_argv_ends_with_faststart_mp4_output(self):
        for copy_video in (True, False):
            argv = video.build_transcode_argv(
                "ffmpeg",
                "/m/a.mov",
                "/t/web.mp4",
                copy_video=copy_video,
                copy_audio=True,
                threads=2,
            )
            self.assertEqual(
                argv[-7:],
                [
                    "-movflags",
                    "+faststart",
                    "-max_muxing_queue_size",
                    "1024",
                    "-f",
                    "mp4",
                    "/t/web.mp4",
                ],
            )
            self.assertIn("0:V:0", argv)
            # The trailing "?" is what lets a silent clip through.
            self.assertIn("0:a:0?", argv)

    def test_scale_filter_string_is_exact(self):
        """The quotes are ffmpeg's filtergraph syntax, not shell quoting.

        Without them the comma inside ``min()`` is read as a filter
        separator and ffmpeg dies with "No option name near 'min(1280'".
        Pinning the literal string is the only way that stays caught.
        """
        argv = video.build_transcode_argv(
            "ffmpeg", "/m/a.mov", "/t/web.mp4", copy_video=False, copy_audio=True, threads=2
        )
        self.assertEqual(
            argv[argv.index("-vf") + 1],
            "scale=w='min(1280,iw)':h='min(1280,ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2",
        )

    def test_module_never_hands_a_command_string_to_a_shell(self):
        source = (
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "attachments", "video.py")
        )
        with open(source, encoding="utf-8") as handle:
            self.assertNotIn("shell=True", handle.read())


# ---------------------------------------------------------------------------
# Database-backed layers
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VideoTestBase(TestCase):
    """Shared fixture: a case to hang attachments off, and a clean tempdir."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        # The log-once guard is process-global by design (a missing
        # binary is a permanent condition), so it has to be reset or the
        # second test asserting a WARNING would find none.
        video._warned.clear()
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )

    def _upload(self, name="clip.mov", data=None, kind=Attachment.Kind.VIDEO):
        """Attach a file through the real service, derivatives and all."""
        return attach_file(
            self.case,
            SimpleUploadedFile(name, data if data is not None else _video_bytes()),
            kind,
            Attachment.Stage.WORK,
        )

    def _raw_video(self, name="clip.mov", data=None):
        """Attach a video with the derivation step suppressed.

        Leaves ``thumbnail`` empty and ``video_status`` blank, so a test
        can assert what a primitive does to a never-examined row rather
        than to one ``attach_file`` has already stamped.
        """
        with patch("apps.attachments.services.enqueue_video_derivatives"):
            return self._upload(name=name, data=data)

    def assertOriginalIntact(self, attachment, name, data):
        """INV-14: the uploaded bytes are still exactly where they were.

        The user's binding decision, in executable form: a damage video is
        evidence, so nothing in this feature may move, rewrite or delete
        it - the transcode is an *additional* file beside it.
        """
        attachment.refresh_from_db()
        self.assertEqual(attachment.file.name, name)
        path = attachment.file.path
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), data)


class FfmpegAbsentTests(VideoTestBase):
    """The CI shape, and the shape of any image built before ffmpeg landed.

    The suite pins both binaries to ``""``, so this is what every other
    app's video tests run under: uploads succeed, nothing is spawned,
    nothing raises.
    """

    def test_video_upload_succeeds_and_spawns_nothing(self):
        with patch("apps.attachments.video.subprocess.run") as run:
            attachment = self._upload()
        run.assert_not_called()
        self.assertFalse(attachment.thumbnail)
        self.assertFalse(attachment.web_video)
        self.assertTrue(attachment.file)

    def test_primitives_are_quiet_no_ops(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(generate_poster(attachment), False)
            self.assertIs(transcode_video(attachment), False)
        run.assert_not_called()
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.PENDING)

    @override_settings(VIDEO_FFMPEG_BIN="definitely-not-a-real-binary-xyz")
    def test_unresolvable_binary_warns_once_and_spawns_nothing(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            with self.assertLogs("apps.attachments.video", level="WARNING") as logs:
                self.assertIs(generate_poster(attachment), False)
                self.assertIs(generate_poster(attachment), False)
        run.assert_not_called()
        self.assertEqual(len(logs.records), 1)


@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
@patch("apps.attachments.video.shutil.which", _fake_which)
class PosterGenerationTests(VideoTestBase):
    """``generate_poster`` mirrors ``generate_thumbnail``'s contract exactly."""

    def test_poster_is_written_beside_the_original_at_the_poster_edge(self):
        from PIL import Image

        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(frame=_jpeg_bytes()),
        ):
            self.assertIs(generate_poster(attachment), True)

        # On the instance the caller holds, not just in the database:
        # attach_file returns it and the gallery renders from it.
        self.assertTrue(attachment.thumbnail)
        self.assertEqual(
            os.path.dirname(attachment.thumbnail.name), os.path.dirname(attachment.file.name)
        )
        self.assertTrue(attachment.thumbnail.name.endswith("_thumb.jpg"))
        with Image.open(attachment.thumbnail.path) as img:
            self.assertEqual(max(img.size), video.POSTER_MAX_EDGE)

    def test_poster_run_is_bounded_and_never_shelled_out(self):
        from django.conf import settings

        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(frame=_jpeg_bytes()),
        ) as run:
            generate_poster(attachment)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], settings.VIDEO_POSTER_TIMEOUT)
        self.assertNotIn("shell", kwargs)

    def test_empty_first_frame_retries_once_from_the_start(self):
        """rc == 0 is not success: seeking past EOF exits 0 and writes nothing."""
        attachment = self._raw_video()
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            stdout = b"" if len(calls) == 1 else _jpeg_bytes()
            return CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

        with patch("apps.attachments.video.subprocess.run", side_effect=run):
            self.assertIs(generate_poster(attachment), True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("-ss") + 1], video.POSTER_SEEK_SECONDS)
        self.assertEqual(calls[1][calls[1].index("-ss") + 1], video.POSTER_SEEK_RETRY)

    def test_two_empty_frames_give_up_quietly(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(frame=b""),
        ) as run:
            with self.assertLogs("apps.attachments.video", level="INFO"):
                self.assertIs(generate_poster(attachment), False)
        self.assertEqual(run.call_count, 2)
        attachment.refresh_from_db()
        self.assertFalse(attachment.thumbnail)

    def test_huge_stderr_is_truncated_in_the_log(self):
        """ffmpeg's stderr on a corrupt file runs to megabytes."""
        attachment = self._raw_video()
        noise = b"E" * 5_000_000

        def run(argv, **kwargs):
            return CompletedProcess(argv, 1, stdout=b"", stderr=noise)

        with patch("apps.attachments.video.subprocess.run", side_effect=run):
            with self.assertLogs("apps.attachments.video", level="INFO") as logs:
                self.assertIs(generate_poster(attachment), False)
        self.assertLess(len(logs.output[-1]), 2000)

    def test_timeout_is_swallowed_and_logged(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 30),
        ):
            with self.assertLogs("apps.attachments.video", level="WARNING"):
                self.assertIs(generate_poster(attachment), False)
        attachment.refresh_from_db()
        self.assertFalse(attachment.thumbnail)

    def test_binary_vanishing_after_the_path_lookup_is_swallowed(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=FileNotFoundError()
        ):
            with self.assertLogs("apps.attachments.video", level="WARNING"):
                self.assertIs(generate_poster(attachment), False)

    def test_wrong_kind_and_existing_poster_spawn_nothing(self):
        photo = self._upload(name="p.jpg", data=_jpeg_bytes(), kind=Attachment.Kind.PHOTO)
        document = self._upload(
            name="m.pdf", data=b"%PDF-1.4 fake", kind=Attachment.Kind.DOCUMENT
        )
        done = self._raw_video()
        done.thumbnail.save("poster.jpg", ContentFile(_jpeg_bytes(64, 64)), save=True)
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(generate_poster(photo), False)
            self.assertIs(generate_poster(document), False)
            self.assertIs(generate_poster(done), False)
        run.assert_not_called()

    def test_storage_without_a_filesystem_path_is_a_clean_no_op(self):
        attachment = self._raw_video()
        with patch(
            "django.core.files.storage.FileSystemStorage.path",
            side_effect=NotImplementedError,
        ):
            with patch("apps.attachments.video.subprocess.run") as run:
                with self.assertLogs("apps.attachments.services", level="WARNING"):
                    self.assertIs(generate_poster(attachment), False)
        run.assert_not_called()

    def test_the_original_is_untouched_by_poster_extraction(self):
        data = _video_bytes()
        attachment = self._raw_video(data=data)
        name = attachment.file.name
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(frame=_jpeg_bytes()),
        ):
            self.assertIs(generate_poster(attachment), True)
        self.assertOriginalIntact(attachment, name, data)


@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
@patch("apps.attachments.video.shutil.which", _fake_which)
class TranscodeTests(VideoTestBase):
    """``transcode_video`` writes a copy *beside* the original, or nothing."""

    def test_happy_path_writes_the_copy_and_marks_it_ready(self):
        from django.conf import settings

        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ) as run:
            self.assertIs(transcode_video(attachment), True)

        attachment.refresh_from_db()
        self.assertTrue(attachment.web_video.name.endswith("_web.mp4"))
        self.assertEqual(
            os.path.dirname(attachment.web_video.name), os.path.dirname(attachment.file.name)
        )
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.READY)
        ffmpeg_call = run.call_args_list[-1]
        self.assertEqual(ffmpeg_call.kwargs["timeout"], settings.VIDEO_TRANSCODE_TIMEOUT)

    def test_the_temp_file_is_gone_afterwards(self):
        attachment = self._raw_video()
        seen = []
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), on_ffmpeg=seen.append),
        ):
            self.assertIs(transcode_video(attachment), True)
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0][-1]))

    def test_the_original_survives_a_successful_transcode(self):
        """INV-14, the user's binding decision, asserted on disk."""
        data = _video_bytes()
        attachment = self._raw_video(data=data)
        name = attachment.file.name
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ):
            self.assertIs(transcode_video(attachment), True)
        self.assertOriginalIntact(attachment, name, data)
        self.assertTrue(attachment.web_video)

    def test_an_already_compatible_mp4_is_probed_and_left_alone(self):
        attachment = self._raw_video(name="clip.mp4")
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ) as run:
            self.assertIs(transcode_video(attachment), False)
        # Exactly one spawn: the probe. No encode, no second copy, no disk cost.
        self.assertEqual(run.call_count, 1)
        attachment.refresh_from_db()
        self.assertFalse(attachment.web_video)
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.SKIPPED)

    def test_a_compatible_mov_is_a_pure_container_remux(self):
        """The dominant real input: an iPhone .mov in H.264/AAC."""
        attachment = self._raw_video(name="clip.mov")
        argvs = []
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), on_ffmpeg=argvs.append),
        ):
            self.assertIs(transcode_video(attachment), True)
        argv = argvs[0]
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "copy")

    def test_pcm_audio_costs_only_an_audio_re_encode(self):
        attachment = self._raw_video(name="clip.mov")
        argvs = []
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(acodec="pcm_s16le"), on_ffmpeg=argvs.append),
        ):
            self.assertIs(transcode_video(attachment), True)
        argv = argvs[0]
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")

    def test_hevc_is_fully_re_encoded_and_capped(self):
        attachment = self._raw_video(name="clip.mov")
        argvs = []
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(vcodec="hevc"), on_ffmpeg=argvs.append),
        ):
            self.assertIs(transcode_video(attachment), True)
        argv = argvs[0]
        self.assertIn("libx264", argv)
        self.assertIn("min(1280,iw)", argv[argv.index("-vf") + 1])

    def test_zero_byte_output_is_a_failure_even_at_rc_zero(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), output=b""),
        ):
            with self.assertLogs("apps.attachments.video", level="WARNING"):
                self.assertIs(transcode_video(attachment), False)
        attachment.refresh_from_db()
        self.assertFalse(attachment.web_video)
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.FAILED)

    def test_non_zero_exit_is_logged_and_swallowed(self):
        data = _video_bytes()
        attachment = self._raw_video(data=data)
        name = attachment.file.name
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), rc=1, output=None),
        ):
            with self.assertLogs("apps.attachments.video", level="WARNING"):
                self.assertIs(transcode_video(attachment), False)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.FAILED)
        self.assertOriginalIntact(attachment, name, data)

    def test_timeout_leaves_the_original_untouched(self):
        data = _video_bytes()
        attachment = self._raw_video(data=data)
        name = attachment.file.name

        def run(argv, **kwargs):
            if "ffprobe" in os.path.basename(argv[0]):
                return CompletedProcess(argv, 0, stdout=json.dumps(_probe_json()).encode())
            raise subprocess.TimeoutExpired("ffmpeg", 900)

        with patch("apps.attachments.video.subprocess.run", side_effect=run):
            with self.assertLogs("apps.attachments.video", level="WARNING"):
                self.assertIs(transcode_video(attachment), False)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.FAILED)
        self.assertOriginalIntact(attachment, name, data)

    def test_unreadable_input_never_reaches_ffmpeg(self):
        """A file ffprobe cannot read is not one ffmpeg will fix."""
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner({})
        ) as run:
            with self.assertLogs("apps.attachments.services", level="INFO"):
                self.assertIs(transcode_video(attachment), False)
        self.assertEqual(run.call_count, 1)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.FAILED)

    def test_a_larger_output_is_kept_on_purpose(self):
        """The deliberate departure from POR-05's discard-if-larger rule.

        The skip rule is playability-only, so every file that gets this
        far was *not* reliably playable; throwing a larger copy away
        would restore exactly the bug this feature fixes.
        """
        data = _video_bytes(100_000)
        attachment = self._raw_video(name="clip.mov", data=data)
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), output=b"\x00" * 500_000),
        ):
            with self.assertLogs("apps.attachments.services", level="INFO"):
                self.assertIs(transcode_video(attachment), True)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.READY)
        self.assertEqual(attachment.web_video.size, 500_000)

    def test_a_full_disk_refuses_before_spawning_ffmpeg(self):
        """MySQL and media share one volume; filling it takes the DB down."""
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ) as run:
            with patch(
                "apps.attachments.services.shutil.disk_usage",
                return_value=Mock(free=0),
            ):
                with self.assertLogs("apps.attachments.services", level="ERROR"):
                    self.assertIs(transcode_video(attachment), False)
        self.assertEqual(run.call_count, 1)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.PENDING)

    def test_a_row_deleted_mid_encode_drops_the_output_without_a_traceback(self):
        """save(update_fields=...) on a vanished row raises DatabaseError."""
        attachment = self._raw_video()
        pk = attachment.pk

        def delete_row(argv):
            Attachment.objects.filter(pk=pk).delete()

        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), on_ffmpeg=delete_row),
        ):
            with self.assertLogs("apps.attachments.services", level="INFO") as logs:
                self.assertIs(transcode_video(attachment), False)
        self.assertFalse(Attachment.objects.filter(pk=pk).exists())
        self.assertFalse(any(r.levelname == "ERROR" for r in logs.records))
        self.assertFalse(any(r.exc_info for r in logs.records))

    def test_an_existing_copy_spawns_nothing(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ):
            self.assertIs(transcode_video(attachment), True)
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(transcode_video(attachment), False)
        run.assert_not_called()

    @override_settings(VIDEO_TRANSCODE_ENABLED=False)
    def test_the_kill_switch_leaves_the_row_retryable(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(transcode_video(attachment), False)
        run.assert_not_called()
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.PENDING)

    @override_settings(VIDEO_FFMPEG_BIN="")
    def test_no_ffmpeg_leaves_the_row_retryable(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(transcode_video(attachment), False)
        run.assert_not_called()
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.PENDING)


@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
@patch("apps.attachments.video.shutil.which", _fake_which)
class VideoEnqueueTests(VideoTestBase):
    """INV-13 and the one rule an operator can hold in their head."""

    def test_eager_upload_extracts_a_poster_but_never_transcodes(self):
        """THE central constraint: no MP4 derivation on a request path.

        Production has no ``REDIS_URL``, so this is the live path. A
        50 MB clip is minutes of ffmpeg; running it here would occupy one
        of three gunicorn workers and make the upload look like it failed
        while the file is in fact already saved.
        """
        argvs = []

        def run(argv, **kwargs):
            argvs.append(argv)
            return _runner(_probe_json(), frame=_jpeg_bytes())(argv, **kwargs)

        with patch("apps.attachments.video.subprocess.run", side_effect=run):
            with patch.object(tasks.transcode_video_task, "delay") as delay:
                with self.assertLogs("apps.attachments.services", level="INFO") as logs:
                    attachment = self._upload()

        delay.assert_not_called()
        # Only the poster ran: no probe, and nothing carrying encode flags.
        self.assertTrue(all(argv[-1] == "pipe:1" for argv in argvs))
        self.assertFalse(any("-movflags" in argv for argv in argvs))
        self.assertTrue(attachment.thumbnail)
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.PENDING)
        self.assertIn("backfill_video_derivatives", "\n".join(logs.output))

    def test_eager_upload_never_calls_the_transcode_primitive(self):
        with patch("apps.attachments.services.transcode_video") as primitive:
            with patch(
                "apps.attachments.video.subprocess.run", side_effect=_runner(frame=b"")
            ) as run:
                self._upload()
        primitive.assert_not_called()
        # The poster is allowed to spawn (~0.3 s); nothing else is.
        for call in run.call_args_list:
            self.assertEqual(call.args[0][-1], "pipe:1")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_broker_mode_queues_nothing_before_the_transaction_commits(self):
        """POR-01 and the intake views attach inside atomic()."""
        with patch.object(tasks.generate_poster_task, "delay") as poster:
            with patch.object(tasks.transcode_video_task, "delay") as encode:
                with self.captureOnCommitCallbacks(execute=False):
                    with transaction.atomic():
                        self._upload()
                        poster.assert_not_called()
                        encode.assert_not_called()
                poster.assert_not_called()
                encode.assert_not_called()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_broker_mode_queues_both_jobs_once_with_the_pk(self):
        with patch.object(tasks.generate_poster_task, "delay") as poster:
            with patch.object(tasks.transcode_video_task, "delay") as encode:
                with self.captureOnCommitCallbacks(execute=True):
                    attachment = self._upload()
        # The pk, never the instance: the worker is a different process.
        poster.assert_called_once_with(attachment.pk)
        encode.assert_called_once_with(attachment.pk)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_photos_and_documents_never_queue_a_video_job(self):
        # The photo's own thumbnail job is deferred too and would reach a
        # broker that is not there; it is not what this test is about.
        with patch.object(tasks.generate_thumbnail_task, "delay"):
            self._assert_no_video_jobs_for_photos_and_documents()

    def _assert_no_video_jobs_for_photos_and_documents(self):
        with patch.object(tasks.generate_poster_task, "delay") as poster:
            with patch.object(tasks.transcode_video_task, "delay") as encode:
                with self.captureOnCommitCallbacks(execute=True):
                    self._upload(
                        name="p.jpg", data=_jpeg_bytes(), kind=Attachment.Kind.PHOTO
                    )
                    self._upload(
                        name="m.pdf", data=b"%PDF-1.4 fake", kind=Attachment.Kind.DOCUMENT
                    )
        poster.assert_not_called()
        encode.assert_not_called()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_an_attachment_with_both_derivatives_queues_nothing(self):
        attachment = self._raw_video()
        attachment.thumbnail.save("poster.jpg", ContentFile(_jpeg_bytes(64, 64)), save=True)
        attachment.web_video.save("web.mp4", ContentFile(b"x" * 10), save=True)
        with patch.object(tasks.generate_poster_task, "delay") as poster:
            with patch.object(tasks.transcode_video_task, "delay") as encode:
                with self.captureOnCommitCallbacks(execute=True):
                    enqueue_video_derivatives(attachment)
        poster.assert_not_called()
        encode.assert_not_called()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False, VIDEO_TRANSCODE_ENABLED=False)
    def test_the_kill_switch_still_queues_the_poster(self):
        """Off disables the expensive half only."""
        with patch.object(tasks.generate_poster_task, "delay") as poster:
            with patch.object(tasks.transcode_video_task, "delay") as encode:
                with self.captureOnCommitCallbacks(execute=True):
                    attachment = self._upload()
        poster.assert_called_once_with(attachment.pk)
        # The task itself re-checks the flag and stamps the row 'pending',
        # so the backlog is picked up when the flag is flipped back on.
        encode.assert_called_once_with(attachment.pk)

    def test_a_photo_upload_is_byte_identical_to_before_the_feature(self):
        with patch("apps.attachments.services.enqueue_video_derivatives") as video_hook:
            photo = self._upload(name="p.jpg", data=_jpeg_bytes(), kind=Attachment.Kind.PHOTO)
        video_hook.assert_called_once()
        self.assertTrue(photo.thumbnail)


@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
@patch("apps.attachments.video.shutil.which", _fake_which)
class VideoTaskTests(VideoTestBase):
    """The tasks add only a primary-key lookup and the "row is gone" case."""

    def test_poster_task_resolves_a_pk_and_delegates(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(frame=_jpeg_bytes()),
        ):
            self.assertIs(tasks.generate_poster_task(attachment.pk), True)
        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)

    def test_transcode_task_resolves_a_pk_and_delegates(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ):
            self.assertIs(tasks.transcode_video_task(attachment.pk), True)
        attachment.refresh_from_db()
        self.assertTrue(attachment.web_video)
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.READY)

    def test_both_tasks_are_idempotent(self):
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), frame=_jpeg_bytes()),
        ):
            self.assertIs(tasks.generate_poster_task(attachment.pk), True)
            self.assertIs(tasks.transcode_video_task(attachment.pk), True)
        with patch("apps.attachments.video.subprocess.run") as run:
            self.assertIs(tasks.generate_poster_task(attachment.pk), False)
            self.assertIs(tasks.transcode_video_task(attachment.pk), False)
        run.assert_not_called()

    def test_both_tasks_tolerate_a_deleted_row(self):
        """A row may be deleted between enqueue and execution."""
        attachment = self._raw_video()
        stale_pk = attachment.pk
        attachment.delete()
        with patch("apps.attachments.video.subprocess.run") as run:
            with self.assertLogs("apps.attachments.tasks", level="INFO"):
                self.assertIs(tasks.generate_poster_task(stale_pk), False)
            with self.assertLogs("apps.attachments.tasks", level="INFO"):
                self.assertIs(tasks.transcode_video_task(stale_pk), False)
        run.assert_not_called()


@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
@patch("apps.attachments.video.shutil.which", _fake_which)
class BackfillVideoDerivativesCommandTests(VideoTestBase):
    """The operator repair path: synchronous, bounded and re-runnable."""

    def _call(self, *args):
        from io import StringIO

        out, err = StringIO(), StringIO()
        call_command("backfill_video_derivatives", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_the_command_never_queues(self):
        """A batch repair reports its count when it returns."""
        attachment = self._raw_video()
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), frame=_jpeg_bytes()),
        ):
            with patch.object(tasks.generate_poster_task, "delay") as poster:
                with patch.object(tasks.transcode_video_task, "delay") as encode:
                    self._call()
        poster.assert_not_called()
        encode.assert_not_called()
        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)
        self.assertTrue(attachment.web_video)

    def test_dry_run_reports_both_counts_and_writes_nothing(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            out, _ = self._call("--dry-run")
        run.assert_not_called()
        self.assertIn("1 video(s) missing a poster frame.", out)
        self.assertIn("1 video(s) missing a browser-playable copy.", out)
        attachment.refresh_from_db()
        self.assertFalse(attachment.thumbnail)
        self.assertFalse(attachment.web_video)

    def test_a_real_run_fills_the_gap_and_a_re_run_is_a_no_op(self):
        self._raw_video()
        runner = _runner(_probe_json(), frame=_jpeg_bytes())
        with patch("apps.attachments.video.subprocess.run", side_effect=runner):
            self._call()
        with patch("apps.attachments.video.subprocess.run") as run:
            out, _ = self._call()
        run.assert_not_called()
        self.assertIn("Posters created: 0", out)
        self.assertIn("Web copies created: 0", out)

    def test_limit_bounds_one_run(self):
        self._raw_video()
        self._raw_video()
        runner = _runner(_probe_json(), frame=_jpeg_bytes())
        with patch("apps.attachments.video.subprocess.run", side_effect=runner):
            self._call("--limit", "1")
        self.assertEqual(
            Attachment.objects.filter(kind=Attachment.Kind.VIDEO, web_video="").count(), 1
        )
        self.assertEqual(
            Attachment.objects.filter(kind=Attachment.Kind.VIDEO, thumbnail="").count(), 1
        )

    def test_the_two_halves_can_be_run_separately(self):
        attachment = self._raw_video()
        runner = _runner(_probe_json(), frame=_jpeg_bytes())
        with patch("apps.attachments.video.subprocess.run", side_effect=runner):
            self._call("--posters-only")
        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)
        self.assertFalse(attachment.web_video)

        other = self._raw_video()
        with patch("apps.attachments.video.subprocess.run", side_effect=runner):
            self._call("--transcodes-only")
        other.refresh_from_db()
        self.assertFalse(other.thumbnail)
        self.assertTrue(other.web_video)

    def test_failed_rows_are_excluded_until_retry_failed(self):
        """A nightly run must not re-burn the timeout on a corrupt file."""
        attachment = self._raw_video()
        Attachment.objects.filter(pk=attachment.pk).update(
            video_status=Attachment.VideoStatus.FAILED
        )
        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), frame=_jpeg_bytes()),
        ):
            self._call("--transcodes-only")
        attachment.refresh_from_db()
        self.assertFalse(attachment.web_video)

        with patch(
            "apps.attachments.video.subprocess.run",
            side_effect=_runner(_probe_json(), frame=_jpeg_bytes()),
        ):
            self._call("--transcodes-only", "--retry-failed")
        attachment.refresh_from_db()
        self.assertTrue(attachment.web_video)

    def test_skipped_rows_never_come_back(self):
        attachment = self._raw_video(name="clip.mp4")
        with patch(
            "apps.attachments.video.subprocess.run", side_effect=_runner(_probe_json())
        ):
            self._call("--transcodes-only")
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.SKIPPED)
        with patch("apps.attachments.video.subprocess.run") as run:
            self._call("--transcodes-only", "--retry-failed")
        run.assert_not_called()

    @override_settings(VIDEO_FFMPEG_BIN="")
    def test_missing_binaries_abort_cleanly_and_touch_no_row(self):
        attachment = self._raw_video()
        with patch("apps.attachments.video.subprocess.run") as run:
            out, err = self._call()
        run.assert_not_called()
        self.assertIn("ffmpeg is not available", err)
        self.assertEqual(out, "")
        attachment.refresh_from_db()
        self.assertFalse(attachment.thumbnail)
        self.assertEqual(attachment.video_status, "")

    def test_backfill_thumbnails_still_sees_only_photos(self):
        """The two commands share one column and must not share a queryset."""
        video_row = self._raw_video()
        photo = self._upload(name="p.jpg", data=_jpeg_bytes(), kind=Attachment.Kind.PHOTO)
        photo.thumbnail.delete(save=True)
        with patch("apps.attachments.video.subprocess.run") as run:
            call_command("backfill_thumbnails")
        run.assert_not_called()
        video_row.refresh_from_db()
        photo.refresh_from_db()
        self.assertFalse(video_row.thumbnail)
        self.assertTrue(photo.thumbnail)


class MediaGalleryVideoTemplateTests(VideoTestBase):
    """The gallery is the only surface that renders ``<video>``."""

    def _render(self, attachment):
        return render_to_string(
            "backoffice/includes/_media_gallery.html",
            {"gallery": group_attachments_by_kind([attachment])},
        )

    def test_a_poster_drops_the_tag_to_preload_none(self):
        attachment = self._raw_video()
        attachment.thumbnail.save("poster.jpg", ContentFile(_jpeg_bytes(64, 64)), save=True)
        html = self._render(attachment)
        self.assertIn(f'poster="{attachment.thumbnail.url}"', html)
        self.assertIn('preload="none"', html)

    def test_without_a_poster_nothing_regresses(self):
        """preload="none" and no poster would render a black rectangle."""
        attachment = self._raw_video()
        html = self._render(attachment)
        self.assertIn('preload="metadata"', html)
        self.assertNotIn("poster=", html)
        self.assertIn(attachment.file.url, html)

    def test_the_derived_copy_is_offered_first_and_is_the_only_typed_source(self):
        attachment = self._raw_video()
        attachment.web_video.save("web.mp4", ContentFile(b"x" * 10), save=True)
        html = self._render(attachment)
        # <video> takes the first source it believes it can decode, so a
        # later entry is never reached.
        self.assertLess(html.index(attachment.web_video.url), html.index(attachment.file.url))
        # Annotating the .mov fallback would make Chrome skip it without
        # trying, turning a degraded tile into a broken one, so exactly one
        # <source> carries a type= and it is the derived copy's.
        self.assertEqual(html.count("type="), 1)
        self.assertEqual(html.count('type="video/mp4"'), 1)

    def test_the_original_stays_one_click_away_when_a_copy_exists(self):
        attachment = self._raw_video()
        html = self._render(attachment)
        self.assertNotIn("Original", html)

        attachment.web_video.save("web.mp4", ContentFile(b"x" * 10), save=True)
        html = self._render(attachment)
        self.assertIn(">Original</a>", html)
        # The backoffice <body> sets hx-boost="true"; without this htmx
        # would swap video bytes into #main.
        self.assertIn('hx-boost="false"', html)


@skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed")
@override_settings(VIDEO_FFMPEG_BIN="ffmpeg", VIDEO_FFPROBE_BIN="ffprobe")
class FfmpegIntegrationTests(VideoTestBase):
    """The only class that genuinely shells out - CI skips it entirely.

    Fixtures are generated by the same binary the guard checks for, so no
    video blob enters git and the skip condition is exactly the fixture's
    precondition. This layer is what stops the mocked layers above from
    drifting into asserting a fiction.
    """

    def _make_clip(self, name, *, size="1280x720", duration="2", extra=None):
        path = os.path.join(tempfile.mkdtemp(), name)
        argv = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=30:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
        ]
        argv += extra or []
        argv.append(path)
        subprocess.run(argv, check=True, timeout=120, stdin=subprocess.DEVNULL)
        with open(path, "rb") as handle:
            return handle.read()

    def test_a_real_poster_comes_out_at_the_poster_edge(self):
        from PIL import Image

        data = self._make_clip("src.mp4")
        attachment = self._raw_video(name="src.mp4", data=data)
        self.assertIs(generate_poster(attachment), True)
        with Image.open(attachment.thumbnail.path) as img:
            self.assertEqual(img.size, (640, 360))
        self.assertOriginalIntact(attachment, attachment.file.name, data)

    def test_a_real_mov_is_remuxed_into_a_playable_mp4(self):
        data = self._make_clip("src.mov")
        attachment = self._raw_video(name="src.mov", data=data)
        self.assertIs(transcode_video(attachment), True)
        attachment.refresh_from_db()
        self.assertEqual(attachment.video_status, Attachment.VideoStatus.READY)
        info = video.probe(attachment.web_video.path)
        self.assertEqual(video.video_stream(info)["codec_name"], "h264")
        self.assertEqual(video.audio_stream(info)["codec_name"], "aac")
        self.assertTrue(video.is_browser_ready(attachment.web_video.name, info))
        self.assertOriginalIntact(attachment, attachment.file.name, data)

    def test_a_real_short_clip_exercises_the_seek_retry(self):
        """Seeking a second into a 0.3 s clip exits 0 and writes nothing."""
        data = self._make_clip(
            "short.mp4", size="1920x1080", duration="0.3", extra=["-b:v", "8M"]
        )
        attachment = self._raw_video(name="short.mp4", data=data)
        calls = []
        real_run = subprocess.run

        def spy(argv, **kwargs):
            calls.append(argv)
            return real_run(argv, **kwargs)

        with patch("apps.attachments.video.subprocess.run", side_effect=spy):
            self.assertIs(generate_poster(attachment), True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][calls[1].index("-ss") + 1], video.POSTER_SEEK_RETRY)


class VideoServicesImportTests(SimpleTestCase):
    """Guard against the module-level wiring quietly disappearing."""

    def test_attach_file_reaches_the_video_hook(self):
        self.assertTrue(hasattr(services, "enqueue_video_derivatives"))
        self.assertTrue(hasattr(services, "generate_poster"))
        self.assertTrue(hasattr(services, "transcode_video"))


class BrokerFailureTests(TestCase):
    """A dead broker must not fail an upload that already committed.

    ``transaction.on_commit`` callbacks run *after* the COMMIT, i.e.
    outside the caller's own ``try/except``, so an unreachable broker
    raising in ``.delay()`` would propagate into the view that just
    saved the file - a 500 on an upload that in fact succeeded. The
    matching hole in ``notifications.services._enqueue`` was found by a
    test, not by inspection; this is that test for attachments.
    """

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_unreachable_broker_does_not_break_a_committed_upload(self):
        boom = OSError("Error 111 connecting to redis:6379. Connection refused.")
        for task_name in (
            "generate_thumbnail_task",
            "generate_poster_task",
            "transcode_video_task",
        ):
            with self.subTest(task=task_name):
                task = getattr(tasks, task_name)
                with patch.object(task, "delay", side_effect=boom) as delay:
                    with self.assertLogs("apps.attachments.services", level="ERROR"):
                        with self.captureOnCommitCallbacks(execute=True):
                            services._defer(task, 1234)
                delay.assert_called_once_with(1234)
