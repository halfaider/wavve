import platform
import pathlib
import traceback
import subprocess
import urllib.parse
import datetime
import re
import stat
import functools
from io import BytesIO

import requests
import webvtt

from wv_tool import WVDownloader
from wv_tool.lib.mpegdash.parser import MPEGDASHParser
from wv_tool.tool import MP4DECRYPT
from .setup import P, F


PATH_DATA = pathlib.Path(F.config['path_data'])
BIN_DIR = pathlib.Path(__file__).parent / 'bin' / platform.system()
if platform.machine() == 'aarch64':
    BIN_DIR = BIN_DIR.parent / 'LinuxArm'
RE_EXECUTE = BIN_DIR / 'N_m3u8DL-RE'
if RE_EXECUTE.exists():
    mode = RE_EXECUTE.stat().st_mode
    RE_EXECUTE.chmod(mode | stat.S_IEXEC)
# Windows에서 muxer의 bin_path 지정이 잘 안돼서 동일 경로에 저장
if platform.system() == 'Windows':
    RE_EXECUTE = RE_EXECUTE.with_name('N_m3u8DL-RE.exe')
    FFMPEG = BIN_DIR / 'ffmpeg.exe'
else:
    FFMPEG = '/usr/bin/ffmpeg'


class REDownloader(WVDownloader):

    def downloadable(func: callable) -> callable:
        @functools.wraps(func)
        def wrapper(self, *args, **kwds) -> bool:
            try:
                if not self.check_file_path():
                    return False
                command = self.get_command(func.__name__)
                return self.execute_command(command)
            except Exception:
                self.logger.error(traceback.format_exc())
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
        result = super().download()
        self.end_time = datetime.datetime.now()
        self.download_time = self.end_time - self.start_time
        return result

    def get_command(self, what_for: str = 'download_m3u8') -> list:
        output_filepath = pathlib.Path(self.output_filepath)
        plugin_ffmpeg = F.PluginManager.get_plugin_instance('ffmpeg')
        if plugin_ffmpeg:
            FFMPEG = pathlib.Path(plugin_ffmpeg.ModelSetting.get('ffmpeg_path'))
        match what_for:
            case 'download_mpd':
                # 웨이브는 특정 CDN에서 invalid XML로 응답함
                mpd_file = output_filepath.with_suffix('.mpd')
                MPEGDASHParser.write(self.mpd, str(mpd_file))
                # 실시간 decrypt 시 shaka-packager를 권장하나 윈도우에서 오작동
                command = [
                    str(RE_EXECUTE), str(mpd_file),
                    '--base-url', self.mpd_base_url,
                    '--decryption-binary-path', MP4DECRYPT, '--mp4-real-time-decryption',
                    '--mux-after-done', f'format=mkv:muxer=mkvmerge',
                ]
                for key in self.key:
                    command.extend(['--key', f'{key["kid"]}:{key["key"]}'])
            case 'download_m3u8':
                command = [str(RE_EXECUTE), self.mpd_url]
        command.extend([
            '--tmp-dir', self.temp_dir, '--save-dir', self.output_dir, '--save-name', output_filepath.stem,
            '--auto-select', '--concurrent-download', '--log-level', 'INFO', '--no-log', '--write-meta-json', 'False',
            '--ffmpeg-binary-path', str(FFMPEG),
        ])
        for k, v in self.mpd_headers.items():
            command.extend(['-H', f'{k}: {v}'])
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
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.parse_re_stdout(process)
        try:
            process.wait(timeout=3600)
        except:
            self.logger.error(traceback.format_exc())
            process.kill()
            return False
        if process.returncode == 0:
            return True
        else:
            self.logger.warning(f'Process exit code: {process.returncode}')
            return False

    def parse_re_stdout(self, process: subprocess.Popen) -> None:
        ansi_ptn = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        for line in iter(process.stdout.readline, b''):
            try:
                if getattr(self, '_stop_flag', self._WVDownloader__stop_flag):
                    self.logger.debug(f'Stop downloading...')
                    process.terminate()
                    return False
                try:
                    msg = line.decode('utf-8').strip()
                except UnicodeDecodeError as ude:
                    msg = line.decode('cp949').strip()
                msg = ansi_ptn.sub('', msg)
                if not msg:
                    continue
                self.logger.debug(msg)
            except:
                self.logger.error(traceback.format_exc())


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
    url_parts: urllib.parse.ParseResult = urllib.parse.urlparse(url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Host": url_parts.hostname,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    srt_file = pathlib.Path(video_file_path).with_suffix(f'.{lang}.srt')
    try:
        response = requests.request('GET', url, headers=headers, timeout=300)
        if response.status_code == 200:
            vtt = webvtt.from_buffer(BytesIO(response.content))
            with open(srt_file, 'w') as f:
                vtt.write(f, format='srt')
        else:
            P.logger.error(f'Downloading subtitle failed: {str(srt_file)}')
    except:
        P.logger.error(traceback.format_exc())
