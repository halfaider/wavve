import os
import re
import stat
import shutil
import logging
import pathlib
import datetime
import platform
import functools
import subprocess
import urllib.parse

from io import BytesIO

import webvtt

from wv_tool import WVDownloader
from wv_tool.lib.mpegdash.parser import MPEGDASHParser
from wv_tool.tool import MP4DECRYPT, MKVMERGE
from support_site import SupportWavve
from .setup import P, F


PATH_DATA = pathlib.Path(F.config['path_data'])
BIN_DIR = pathlib.Path(__file__).parent / 'bin' / platform.system()
if platform.machine() == 'aarch64':
    BIN_DIR = BIN_DIR.parent / 'LinuxArm'
RE_EXECUTE = BIN_DIR / 'N_m3u8DL-RE'
if RE_EXECUTE.exists():
    mode = RE_EXECUTE.stat().st_mode
    RE_EXECUTE.chmod(mode | stat.S_IEXEC)
if platform.system() == 'Windows':
    RE_EXECUTE = RE_EXECUTE.with_name('N_m3u8DL-RE.exe')
    FFMPEG = BIN_DIR / 'ffmpeg.exe'
    MP4DECRYPT_ = BIN_DIR / 'mp4decrypt.exe'
    MKVMERGE_ = BIN_DIR / 'mkvmerge.exe'
    if not MKVMERGE_.exists():
        MKVMERGE_.symlink_to(MKVMERGE)
else:
    FFMPEG = pathlib.Path('/usr/bin/ffmpeg')
    MP4DECRYPT_ = BIN_DIR / 'mp4decrypt'
if not MP4DECRYPT_.exists():
    MP4DECRYPT_.unlink(missing_ok=True)
    MP4DECRYPT_.symlink_to(MP4DECRYPT)


class REDownloader(WVDownloader):

    def downloadable(func: callable) -> callable:
        @functools.wraps(func)
        def wrapper(self, *args, **kwds) -> bool:
            try:
                if not self.check_file_path():
                    return False
                command = self.get_command(func.__name__)
                return self.execute_command(command)
            except Exception as e:
                self.logger.exception(str(e))
            finally:
                func(self, *args, **kwds)
            return False
        return wrapper

    @downloadable
    def download_mpd(self) -> bool:
        '''override'''
        pathlib.Path(self.output_filepath).with_suffix('.mpd').unlink(missing_ok=True)
        return False

    @downloadable
    def download_m3u8(self) -> bool:
        '''override'''
        return False

    def download(self) -> bool:
        '''override'''
        try:
            mpd_url = urllib.parse.urlparse(self.mpd_url)
            self.mpd_headers['Host'] = mpd_url.netloc
            self.start_time = datetime.datetime.now()
            self.set_status("READY")
            output = pathlib.Path(self.output_filepath)
            if output.exists():
                self.logger.debug(f"{self.output_filepath} FILE EXIST")
                self.set_status("EXIST_OUTPUT_FILEPATH")
                return False
            pathlib.Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            self.prepare()
            self.set_status("DOWNLOADING")
            if self.streaming_protocol == 'hls':
                result = self.download_m3u8()
            elif self.streaming_protocol == 'dash':
                if not self.mpd:
                    self.get_mpd()
                if self.mpd:
                    self.analysis_mpd()
                self.make_download_info()
                result = self.download_mpd()
            if result and (self.config.get('clean') or True):
                self.clean()
            if self._WVDownloader__stop_flag:
                self.set_status("USER_STOP")
            elif result and self.status == "DOWNLOADING":
                self.set_status("COMPLETED")
            else:
                self.set_status("ERROR")
            return result
        except Exception as e:
            self.logger.exception(repr(e))
        return False

    def get_command(self, what_for: str = 'download_m3u8') -> list:
        output_filepath = pathlib.Path(self.output_filepath)
        plugin_ffmpeg = F.PluginManager.get_plugin_instance('ffmpeg')
        if plugin_ffmpeg:
            FFMPEG = pathlib.Path(plugin_ffmpeg.ModelSetting.get('ffmpeg_path'))
        if which_path := shutil.which(str(FFMPEG), mode=os.F_OK | os.X_OK, path=None):
            FFMPEG = pathlib.Path(which_path)
        else:
            raise Exception(f"Could not execute FFmpeg: {str(FFMPEG)}")
        command = [str(RE_EXECUTE)]
        match what_for:
            case 'download_mpd':
                # 웨이브는 특정 CDN에서 invalid XML로 응답함
                mpd_file = output_filepath.with_suffix('.mpd')
                MPEGDASHParser.write(self.mpd, str(mpd_file))
                '''
                실시간 decrypt 시 shaka-packager를 권장하나 윈도우에서 오작동
                Windows에서 mkvmerge.exe의 bin_path 지정이 제대로 동작하지 않아 RE와 동일 경로에 있어야 함
                '''
                command.extend((str(mpd_file), '--base-url', self.mpd_base_url))
                # --key를 입력하면 --decryption-binary-path 지정이 제대로 동작하지 않아 RE와 동일 경로에 mp4decrypt가 있어야 함
                for key in self.key:
                    command.extend(('--key', f'{key["kid"]}:{key["key"]}'))
            case 'download_m3u8':
                command.append(self.mpd_url)
        if output_filepath.suffix == '.mkv':
            command.extend(('--mux-after-done', 'format=mkv:muxer=mkvmerge'))
        command.extend((
            '--tmp-dir', self.temp_dir,
            '--save-dir', self.output_dir,
            '--save-name', output_filepath.stem,
            '--ffmpeg-binary-path', str(FFMPEG),
            '--decryption-binary-path', MP4DECRYPT,
            '--write-meta-json', 'False',
            '--download-retry-count', '3',
            '--log-level', 'INFO',
            '--mp4-real-time-decryption',
            '--auto-select',
            '--concurrent-download',
            '--no-log',
            '--no-ansi-color',
        ))
        for k, v in self.mpd_headers.items():
            command.extend(('-H', f'{k}: {v}'))
        return command

    def check_file_path(self) -> bool:
        # 파일 이름에 comma 가 있으면 오류: ERROR: cannot open fragments info file
        self.output_filename = self.output_filename.replace(',', '')
        output_filename = pathlib.Path(self.output_filename)
        output_dir = pathlib.Path(self.output_dir)
        output_filepath = output_dir / output_filename
        self.output_filepath = str(output_filepath)

        if output_filepath.exists():
            self.logger.warning(f'Already exists: {str(output_filepath)}')
            self.set_status('EXIST_OUTPUT_FILEPATH')
            return False
        else:
            return True

    def execute_command(self, command: list) -> bool:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf8', errors='ignore')
        self.parse_re_stdout(process)
        try:
            process.wait(timeout=3600)
        except Exception:
            self.logger.exception(command)
            process.kill()
            return False
        if process.returncode == 0:
            return True
        else:
            self.logger.warning(f'Process exit code: {process.returncode}')
            return False


    RE_LOGGING_LEVEL = {
        'WARN': logging.WARNING,
        'INFO': logging.INFO,
        'ERROR': logging.ERROR,
        'DEBUG': logging.DEBUG,
    }
    def parse_re_stdout(self, process: subprocess.Popen) -> None:
        for line in iter(process.stdout.readline, ''):
            try:
                if getattr(self, '_stop_flag', self._WVDownloader__stop_flag):
                    self.logger.debug(f'Stop downloading...')
                    process.terminate()
                    return False
                if not (msg := line.strip()):
                    continue
                match = re.compile('^\d{2}:\d{2}:\d{2}\.\d{3}\s(\w+)\s?:\s(.+)$').search(msg)
                if match:
                    level = match.group(1)
                    message = match.group(2)
                    self.logger.log(self.RE_LOGGING_LEVEL.get(level, 'DEBUG'), message)
            except Exception:
                self.logger.error(line)


def download_webvtts(subtitles: list, video_file_path: str, wanted: list) -> None:
    if not wanted:
        return
    for subtitle in subtitles:
        if 'all' in wanted or subtitle.get('languagecode') in wanted:
            url = subtitle.get('url', None)
            if not url:
                continue
            lang = subtitle.get('languagecode', 'ko')
            download_webvtt(url, lang, video_file_path)


def download_webvtt(url: str, lang: str, video_file_path: str) -> None:
    srt_file = pathlib.Path(video_file_path).with_suffix(f'.{lang}.srt')
    try:
        response = SupportWavve.api.request('GET', url)
        if response.status_code == 200:
            vtt = webvtt.from_buffer(BytesIO(response.content))
            with open(srt_file, 'w') as f:
                vtt.write(f, format='srt')
        else:
            P.logger.error(f'Downloading subtitle failed: {str(srt_file)}')
    except Exception:
        P.logger.exception(url)
