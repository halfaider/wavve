import os
import re
import traceback
import urllib.parse
import pathlib
from io import BytesIO

import flask
import requests
import webvtt

from plugin.create_plugin import PluginBase
from plugin.logic_module_base import PluginModuleBase
from support.expand.ffmpeg import SupportFfmpeg
from tool import ToolUtil
from support_site import SupportWavve
from wv_tool import WVDownloader

from .setup import F, P


name = 'basic'


class ModuleBasic(PluginModuleBase):

    def __init__(self, P: PluginBase) -> None:
        super(ModuleBasic, self).__init__(P, 'setting')
        self.name = name
        self.db_default = {
            f"{self.name}_db_version": "1",
            f"{self.name}_quality": "1080p",
            f"{self.name}_save_path": "{PATH_DATA}" + os.sep + "download",
            f"{self.name}_recent_code": "",
        }
        self.last_data = None

    @property
    def download_headers(self) -> dict:
        return {
        "Accept": "application/json, text/plain, */*",
        'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    }

    def process_menu(self, page_name: str, req: flask.Request) -> flask.Response:
        arg = P.ModelSetting.to_dict()
        if page_name == 'download':
            arg['code'] = req.args.get('code') or P.ModelSetting.get(f'{self.name}_recent_code')
        return flask.render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)

    def process_command(self, command: str, arg1: str, arg2: str, arg3: str, req: flask.Request) -> flask.Response:
        ret = {'ret':'success'}
        match command:
            case 'analyze':
                ret = self.analyze(arg1, quality=arg2) if arg2 else self.analyze(arg1)
            case 'download_start':
                save_path = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))
                if arg3 == 'hls':
                    #logger.warning(os.path.join(F.config['path_data'], P.ModelSetting.get(f"{self.name}_save_path"), arg2))
                    downloader = SupportFfmpeg(
                        SupportWavve.get_prefer_url(arg1),
                        arg2,
                        save_path=save_path,
                        #callback_function=self.ffmpeg_listener,
                        callback_id=f"{P.package_name}",
                        headers=self.download_headers,
                    )
                else:
                    downloader = WVDownloader(
                        {
                            'callback_id': 'wavve_basic',
                            'logger' : P.logger,
                            'mpd_url' : self.last_data['streaming']['play_info']['uri'],
                            'code' : self.last_data['code'],
                            'output_filename' : self.last_data['available']['filename'],
                            'license_headers' : self.last_data['streaming']['play_info']['drm_key_request_properties'],
                            'license_url' : self.last_data['streaming']['play_info']['drm_license_uri'],
                            'mpd_headers': self.last_data['streaming']['play_info']['mpd_headers'],
                            'clean' : False,
                            'folder_tmp': os.path.join(F.config['path_data'], 'tmp'),
                            'folder_output': save_path,
                            'proxies': SupportWavve._SupportWavve__get_proxies(),
                        }
                    )
                # 자막 다운로드
                self.download_webvtts(self.last_data['streaming'].get('subtitles', []), f"{save_path}/{self.last_data['available']['filename']}")
                downloader.start()
            case 'program_page':
                data = SupportWavve.vod_program_contents_programid(arg1, page=int(arg2))
                ret =  {'url_type': 'program', 'page':arg2, 'code':arg1, 'data' : data}
        return flask.jsonify(ret)

    def analyze(self, url: str, quality: str = None) -> dict | None:
        try:
            #logger.debug('analyze :%s', url)
            url_type = None
            code = None
            patterns = {
                'episode': re.compile(r'contentid\=(?P<code>.*?)(\&|$|\#)'),
                'program': re.compile(r'programid\=(PRG_)?(?P<code>.*?)(\&|$|\#)'),
                'movie': re.compile(r'movieid\=(?P<code>.*?)($|\#)'),
            }
            for _type, pattern in patterns.items():
                match = pattern.search(url)
                if match:
                    code = match.group('code')
                    url_type = _type
                    break
            if not code or not url_type:
                if len(url.split('.')) == 2:
                    url_type = 'episode'
                    code = url.strip()
                elif url.startswith('MV_'):
                    url_type = 'movie'
                    code = url.strip()
                elif url.find('_') != -1:
                    url_type = 'program'
                    code = url.strip()
                    code = code.replace('PRG_', '')
            P.logger.debug(f'Analyze {url_type} {code}')
            if not quality:
                quality = P.ModelSetting.get(f"{self.name}_quality")
            match url_type:
                case 'episode':
                    data = SupportWavve.vod_contents_contentid(code)
                    self.last_data = {'episode' : data}
                    contenttype = 'onairvod' if data.get('type', '') == 'onair' else 'vod'
                case 'movie':
                    data = SupportWavve.movie_contents_movieid(code)
                    self.last_data = {'info' : data}
                    contenttype = 'movie'
                case 'program':
                    data = SupportWavve.vod_program_contents_programid(code)
                    P.ModelSetting.set(f"{self.name}_recent_code", code)
                    return {'url_type': url_type, 'page':'1', 'code':code, 'data' : data}
                case _:
                    return {'url_type':'None'}
            action = "hls" if not data.get('drms') else "dash"
            data2 = SupportWavve.streaming(contenttype, code, quality, action=action)
            data3 = {
                'filename': SupportWavve.get_filename(data, quality),
                'preview': (data2['playurl'].find('preview') != -1),
                'current_quality': quality,
                'action': action,
            }
            P.ModelSetting.set(f"{self.name}_recent_code", code)
            self.last_data.update({'url_type': url_type, 'code':code, 'streaming':data2, 'available' : data3})
            return self.last_data
        except Exception as e:
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())

    def plugin_load(self):
        from sjva import Auth
        if Auth.get_auth_status()['ret'] == False:
            raise Exception('auth fail!')

    @classmethod
    def download_webvtts(cls, subtitles: list, video_file_path: str) -> None:
        for subtitle in subtitles:
            lang = subtitle.get('languagecode', 'ko')
            url = subtitle.get('url', None)
            if url:
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
                response = requests.request('GET', url, headers=headers)
                if response.status_code == 200:
                    try:
                        vtt = webvtt.from_buffer(BytesIO(response.content))
                        with open(srt_file, 'w') as f:
                            vtt.write(f, format='srt')
                    except:
                        P.logger.error(traceback.format_exc())
                else:
                    P.logger.error(f'Downloading subtitle failed: {str(srt_file)}')

