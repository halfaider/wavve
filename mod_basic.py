import os
import re
import pathlib

import flask

from plugin.create_plugin import PluginBase
from plugin.logic_module_base import PluginModuleBase
from support.expand.ffmpeg import SupportFfmpeg
from tool import ToolUtil
from support_site import SupportWavve
from wv_tool import WVDownloader

from .setup import F, P
from .downloader import REDownloader, download_webvtts, download_webvtt, set_binary


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
            f"{self.name}_drm": "WV",
            f"{self.name}_subtitle_langs": "all",
            f"{self.name}_hls": "WV",
            f"{self.name}_bin_path": (pathlib.Path(F.config['path_data']) / 'bin').absolute().as_posix()
        }
        self.last_data = None

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
                proxies = SupportWavve.api.get_session().proxies
                if self.last_data['streaming'].get('drm'):
                    # dash
                    drm_key_request_properties = self.last_data['streaming']['play_info'].get('drm_key_request_properties') or ''
                    drm_license_uri = self.last_data['streaming']['play_info'].get('drm_license_uri') or ''
                    if not (drm_key_request_properties and drm_license_uri):
                        P.logger.error(f"Could not download this DRM file: {self.last_data['available']['filename']}")
                        P.logger.error(self.last_data['streaming']['play_info'])
                        return {'ret':'failed'}
                    parameters = {
                        'callback_id': 'wavve_basic',
                        'logger': P.logger,
                        'mpd_url': self.last_data['streaming']['playurl'],
                        'code': self.last_data['code'],
                        'output_filename': self.last_data['available']['filename'],
                        'license_headers': drm_key_request_properties,
                        'license_url': drm_license_uri,
                        'mpd_headers': self.last_data['streaming']['play_info'].get('mpd_headers'),
                        'clean': True,
                        'folder_tmp': os.path.join(F.config['path_data'], 'tmp'),
                        'folder_output': save_path,
                        'proxies': proxies,
                    }
                    downloader_cls = REDownloader if P.ModelSetting.get(f'{self.name}_drm') == 'RE' else WVDownloader
                    downloader = downloader_cls(parameters)
                else:
                    headers = self.last_data['streaming']['play_info'].get('headers')
                    match P.ModelSetting.get(f'{self.name}_hls'):
                        case 'RE':
                            downloader = REDownloader({
                                'callback_id': 'wavve_basic',
                                'logger': P.logger,
                                'mpd_url': self.last_data['streaming']['playurl'],
                                'streaming_protocol': 'hls',
                                'code': self.last_data['code'],
                                'output_filename': self.last_data['available']['filename'],
                                'license_url': None,
                                'mpd_headers': headers,
                                'clean': True,
                                'folder_tmp': os.path.join(F.config['path_data'], 'tmp'),
                                'folder_output': save_path,
                                'proxies': proxies,
                            })
                        case _:
                            downloader = SupportFfmpeg(
                                SupportWavve.get_prefer_url(arg1, headers),
                                arg2,
                                save_path=save_path,
                                callback_id=f"{P.package_name}",
                                headers=headers,
                            )
                # 자막 다운로드
                download_webvtts(
                    self.last_data['streaming'].get('subtitles', []),
                    f"{save_path}/{self.last_data['available']['filename']}",
                    P.ModelSetting.get_list(f'{self.name}_subtitle_langs', delimeter=',')
                )
                downloader.start()
            case 'program_page':
                data = SupportWavve.vod_program_contents_programid(arg1, page=int(arg2))
                ret =  {'url_type': 'program', 'page':arg2, 'code':arg1, 'data' : data}
            case 'download_subtitle':
                save_path = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))
                download_webvtt(arg1, arg2, str(pathlib.Path(save_path) / arg3))
        return flask.jsonify(ret)

    def analyze(self, url: str, quality: str = None) -> dict | None:
        try:
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
            P.logger.exception(str(e))
            return self.last_data

    def plugin_load(self) -> None:
        set_binary()

    def setting_save_after(self, changes: list) -> None:
        '''override'''
        for change in changes:
            match change:
                case 'base_bin_path':
                    set_binary()
