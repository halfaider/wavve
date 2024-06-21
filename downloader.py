import platform
import pathlib
import traceback
import subprocess
import urllib.parse
import logging
import datetime
import re
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
SHAKA_PACKAGER = BIN_DIR / 'packager-linux-arm64'


class REDownloader(WVDownloader):

    def download_mpd(self) -> bool:
        try:
            # 버그: 파일 이름에 comma가 있으면 오류: 우리, 집
            self.output_filename = self.output_filename.replace(',', '')
            output_filename = pathlib.Path(self.output_filename)
            container = output_filename.suffix
            container = container[1:] if container else 'mkv'
            output_dir = pathlib.Path(self.output_dir)
            output_filepath = output_dir / output_filename
            self.output_filepath = str(output_filepath)

            if output_filepath.exists():
                self.logger.warning(f'Already exists: {str(output_filepath)}')
                self.set_status('EXIST_OUTPUT_FILEPATH')
                return False

            mpd_file = output_filepath.with_suffix('.mpd')
            MPEGDASHParser.write(self.mpd, str(mpd_file))

            command = [
                str(RE_EXECUTE), str(mpd_file),
                '--tmp-dir', self.temp_dir, '--save-dir', self.output_dir, '--save-name', output_filename.stem, '-M', container,
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
        except Exception as e:
            self.logger.error(traceback.format_exc())
        finally:
            mpd_file.unlink(missing_ok=True)
        return False

    def download(self) -> bool:
        result = super().download()
        self.end_time = datetime.datetime.now()
        self.download_time = self.end_time - self.start_time
        return result

    def download_m3u8(self) -> bool:
        try:
            # 버그: 파일 이름에 comma가 있으면 오류: 우리, 집
            self.output_filename = self.output_filename.replace(',', '')
            output_filename = pathlib.Path(self.output_filename)
            container = output_filename.suffix
            container = container[1:] if container else 'mkv'
            output_dir = pathlib.Path(self.output_dir)
            output_filepath = output_dir / output_filename
            self.output_filepath = str(output_filepath)

            if output_filepath.exists():
                self.logger.warning(f'Already exists: {str(output_filepath)}')
                self.set_status('EXIST_OUTPUT_FILEPATH')
                return False

            # RE가 세그먼트 주소에 파라미터를 강제로 붙여서 요청하기 때문에 403 에러
            command = [
                str(RE_EXECUTE), self.mpd_url,
                '--tmp-dir', self.temp_dir, '--save-dir', self.output_dir, '--save-name', output_filename.stem, '-M', container,
                '--base-url', self.mpd_base_url, '--write-meta-json', 'False',
                '--ffmpeg-binary-path', '/usr/bin/ffmpeg', '--auto-select',
                '--concurrent-download', '--log-level', 'DEBUG', '--no-log', '--append-url-params', 'False',
            ]
            for k, v in self.mpd_headers.items():
                command.append('-H')
                command.append(f'{k}: {v}')

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
        except Exception as e:
            self.logger.error(traceback.format_exc())
        return False

    def parse_re_stdout(self, process: subprocess.Popen) -> None:
        re_logger = get_logger('N_m3u8DL-RE', str(PATH_DATA / 'log'))
        timestamp_ptn = re.compile(r'^\d{1,2}:\d{2}:\d{2}\.\d+\s(.+)$')
        ansi_ptn = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        progress_ptn = re.compile(r'(Vid|Aud)\s(\d+x\d+|.+)\s\|\s.+\s(\d+)%')
        progress_count = 0
        downloaded = set()
        for line in iter(process.stdout.readline, b''):
            try:
                if getattr(self, '_stop_flag', self._WVDownloader__stop_flag):
                    self.logger.debug(f'Stop downloading...')
                    process.terminate()
                    return False
                msg = line.decode('utf-8').strip()
                msg = ansi_ptn.sub('', msg)
                if not msg:
                    continue
                re_logger.debug(msg)
                match = progress_ptn.match(msg)
                if match:
                    progress_count += 1
                    percent = int(match.group(3))
                    if match.group(3) in downloaded:
                        continue
                    if percent > 99:
                        downloaded.add(match.group(3))
                    self.logger.debug(msg)
                    # 테스트 환경과 실제 도커의 로그 빈도수 차이나는 이유?
                    #if percent > 99 or progress_count > 30:
                    #    self.logger.debug(msg)
                    #    progress_count = 0
                    continue
                match = timestamp_ptn.match(msg)
                if match:
                    self.logger.debug(match.group(1))
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
        response = requests.request('GET', url, headers=headers, timeout=300)
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
    return logger
