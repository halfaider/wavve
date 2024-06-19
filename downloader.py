import os
import platform
import pathlib
import traceback
import subprocess
import urllib.parse
import logging
from io import BytesIO

import requests
import webvtt

from wv_tool import WVDownloader
from wv_tool.lib.mpegdash.parser import MPEGDASHParser
from wv_tool.tool import MP4DECRYPT
from support import SupportSubprocess
from .setup import P, F

BIN_DIR = pathlib.Path(__file__).parent / 'bin' / platform.system()
if platform.machine() == 'aarch64':
    BIN_DIR = BIN_DIR.parent / 'LinuxArm'
RE_EXECUTE = BIN_DIR / 'N_m3u8DL-RE'
SHAKA_PACKAGER = BIN_DIR / 'packager-linux-arm64'


class REDownloader(WVDownloader):

    def download_mpd(self):
        try:
            mpd_file = pathlib.Path(self.output_filepath).with_suffix('.mpd')
            MPEGDASHParser.write(self.mpd, str(mpd_file))
            file_name = str(pathlib.Path(self.output_filename).with_suffix(''))
            # 버그: 파일 이름에 comma가 있으면 오류: 우리, 집
            file_name = file_name.replace(',', '')
            command = [
                str(RE_EXECUTE), str(mpd_file),
                '--tmp-dir', self.temp_dir, '--save-dir', self.output_dir, '--save-name', file_name, '-M', 'mp4',
                '--base-url', self.mpd_base_url, '--write-meta-json', 'False',
                '--decryption-binary-path', SHAKA_PACKAGER, '--use-shaka-packager',
                '--ffmpeg-binary-path', '/usr/bin/ffmpeg', '--mp4-real-time-decryption',
                '--select-video', 'best', '--select-audio', 'best', '--select-subtitle', 'all',
                '--concurrent-download', '--log-level', 'INFO', '--no-log',
            ]
            for k, v in self.mpd_headers.items():
                command.append('-H')
                command.append(f'{k}: {v}')
            for key in self.key:
                command.append('--key')
                command.append(f'{key["kid"]}:{key["key"]}')

            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                re_logger = get_logger('N_m3u8DL-RE', str(pathlib.Path(F.config['path_data']) / 'log'))
                for line in iter(process.stdout.readline, b''):
                    if getattr(self, '_stop_flag', self._WVDownloader__stop_flag):
                        self.logger.debug(f'Stop downloading...')
                        process.terminate()
                        return False
                    msg = line.decode('utf-8').strip()
                    re_logger.debug(msg)
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
        except Exception as e:
            self.logger.error(traceback.format_exc())
        finally:
            mpd_file.unlink(missing_ok=True)
        return False

    def stdout_callback(self, call_id, mode, data):
        self.logger.debug(f'{mode}: {data}')


def download_webvtts(subtitles: list, video_file_path: str) -> None:
    for subtitle in subtitles:
        url = subtitle.get('url', None)
        if not url:
            continue
        lang = subtitle.get('languagecode', 'ko')
        download_webvtt(url, lang, video_file_path)


def download_webvtt(url: str, lang: str, video_file_path: str) -> None:
    url_parts: urllib.parse.ParseResult = urllib.parse.urlparse(url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "ko,ko-KR;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "DNT": "1",
        "Host": url_parts.hostname,
        "Origin": "https://www.wavve.com",
        "Pragma": "no-cache",
        "Referer": "https://www.wavve.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    srt_file = pathlib.Path(video_file_path).with_suffix(f'.{lang}.srt')
    try:
        response = requests.request('GET', url, headers=headers)
        if response.status_code == 200:
            vtt = webvtt.from_buffer(BytesIO(response.content))
            with open(srt_file, 'w') as f:
                vtt.write(f, format='srt')
        else:
            P.logger.error(f'Downloading subtitle failed: {str(srt_file)}')
    except:
        P.logger.error(traceback.format_exc())


def get_logger(name: str = None, log_path: str = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(u'%(message)s')
    file_max_bytes = 5 * 1024 * 1024
    if log_path:
        fileHandler = logging.handlers.RotatingFileHandler(filename=str(pathlib.Path(log_path) / f'{name}.log'), maxBytes=file_max_bytes, backupCount=5, encoding='utf8', delay=True)
        fileHandler.setFormatter(formatter)
        logger.addHandler(fileHandler)
    streamHandler = logging.StreamHandler()
    logger.addHandler(streamHandler)
    return logger