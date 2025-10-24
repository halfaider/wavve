import os
import re
import time
import datetime
from typing import Iterable

import flask
import sqlite3
from flask_sqlalchemy.query import Query
from sqlalchemy import desc, or_

from tool import ToolUtil
from plugin.create_plugin import PluginBase
from plugin.logic_module_base import PluginModuleBase
from plugin.model_base import ModelBase
from support.expand.ffmpeg import SupportFfmpeg
from support_site import SupportWavve, SiteUtil
from wv_tool import WVDownloader

from .setup import F, P
from .downloader import REDownloader, download_webvtts


name = 'recent'


class ModuleRecent(PluginModuleBase):

    def __init__(self, P: PluginBase) -> None:
        super(ModuleRecent, self).__init__(P, 'list', scheduler_desc="웨이브 최근 방송 다운로드")
        self.name = name
        self.db_default = {
            f"{self.name}_db_version": "1.1",
            f"{P.package_name}_{self.name}_last_list_option": "",
            f"{self.name}_interval": "30",
            f"{self.name}_auto_start": "False",
            f"{self.name}_quality": "1080p",
            f"{self.name}_retry_user_abort": "False",
            f"{self.name}_qvod_download": "False",
            f"{self.name}_except_channel": "",
            f"{self.name}_except_program": "",
            f"{self.name}_except_episode_keyword": "특집,비하인드,스페셜,선공개,티저,메이킹,예고",
            f"{self.name}_except_episode_episodetitle": "예고",
            f"{self.name}_page_count": "2",
            f"{self.name}_save_path": "{PATH_DATA}" + os.sep + "download",
            f"{self.name}_download_program_in_qvod": "",
            f"{self.name}_download_mode": "blacklist",
            f"{self.name}_whitelist_program": "",
            f"{self.name}_whitelist_first_episode_download": "True",
            f"{self.name}_ffmpeg_max_count": "4",
            f"{self.name}_2160_receive_1080": "False",
            f"{self.name}_2160_wait_minute": "100",
            f"{self.name}_auto_db_clear": "False",
            f"{self.name}_auto_db_days": "7",
            f"{self.name}_search_genres": "드라마,예능,시사,교양,해외시리즈,애니메이션,스포츠,키즈,시사교양",
            f"{self.name}_search_days": "2",
            f"{self.name}_except_genres": "",
            f"{self.name}_whitelist_genres": "",
            f"{self.name}_drm": "WV",
            f"{self.name}_subtitle_langs": "all",
            f"{self.name}_hls": "WV",
            f"{self.name}_max_retry": "20",
        }
        self.web_list_model = ModelWavveRecent
        self.current_download_count = 0
        self.schedule_running = False
        self.schedule_started_at = datetime.datetime(1900, 1, 1, 0, 0, 0, 0)

    def process_menu(self, page_name: str, req: flask.Request) -> flask.Response:
        arg = P.ModelSetting.to_dict()
        if page_name == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_id())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_id())
        return flask.render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)

    def process_command(self, command: str, arg1: str, arg2: str, arg3: str, req: flask.Request) -> flask.Response:
        ret = {'ret':'success'}
        match command:
            case 'add_condition':
                mode = arg1
                value = arg2
                old_list = P.ModelSetting.get_list(mode, ',')
                old_str = P.ModelSetting.get(mode)
                if value in old_list:
                    ret['msg'] = "이미 설정되어 있습니다."
                    ret['ret'] = "warning"
                else:
                    old_str += f', {value}' if old_str else value
                    P.ModelSetting.set(mode, old_str)
                    ret['msg'] = "추가하였습니다."
            case 'reset_status_of_all':
                for vod in ModelWavveRecent.get_list():
                    vod.completed = False
                    vod.user_abort = False
                    vod.pf_abort = False
                    vod.etc_abort = 0
                    vod.save()
            case 'retrieve':
                vod = ModelWavveRecent.get_by_id(arg1)
                try:
                    self.retrieve_recent_vod(vod, self.retrieve_settings)
                    ret['msg'] = "갱신이 완료되었습니다."
                    vod.etc_abort = 0
                except Exception as e:
                    P.logger.exception(repr(e))
                    vod.retry += 1
                    ret['msg'] = f"갱신하지 못 했습니다: {e}"
                    ret['ret'] = 'warning'
                finally:
                    vod.save()
            case 'delete':
                try:
                    result = ModelWavveRecent.delete_by_id(arg1)
                    if result:
                        ret['msg'] = "삭제되었습니다."
                    else:
                        ret['msg'] = "삭제하지 못 했습니다."
                        ret['ret'] = 'warning'
                except Exception as e:
                    P.logger.exception(repr(e))
                    ret['msg'] = f"삭제할 수 없습니다: {e}"
                    ret['ret'] = 'warning'
            case 'reset_status':
                vod = ModelWavveRecent.get_by_id(arg1)
                vod.completed = False
                vod.user_abort = False
                vod.pf_abort = False
                vod.etc_abort = 0
                vod.ffmpeg_status = -1
                vod.pf = 0
                vod.retry = 0
                vod.save()
                ret['msg'] = "초기화 했습니다."
        return flask.jsonify(ret)

    def get_recent_vods(self) -> list[dict]:
        search_genres = P.ModelSetting.get_list(f'{self.name}_search_genres', delimeter=',')
        search_days = P.ModelSetting.get_int(f'{self.name}_search_days')
        recents, additional_ids = SupportWavve.get_new_vods(days=search_days, genres=search_genres)
        recents = SupportWavve.get_more_new_vods(recents, additional_ids, self.web_list_model, search_days)
        return recents

    def save_recent_vod(self, recent_vod: dict) -> 'ModelWavveRecent':
        vod = ModelWavveRecent.get_episode_by_recent(recent_vod['contentid'])
        if vod:
            vod.set_info(recent_vod)
        else:
            vod = ModelWavveRecent('recent', info=recent_vod)
        vod.save()
        P.logger.debug(f"[{vod.content_type}] [{vod.programtitle}] [{vod.episodenumber}] [{vod.episodetitle}] [{vod.contentid}]")
        return vod

    def save_recent_vods(self, vods: list[dict]) -> None:
        for vod in vods:
            self.save_recent_vod(vod)

    def pick_out_recent_vod(self, vod: 'ModelWavveRecent', settings: dict) -> None:
        if vod.completed:
            vod.etc_abort = 32
            return
        if vod.retry >= P.ModelSetting.get_int(f"{self.name}_max_retry"):
            P.logger.warning(f'Too many retires: {vod.contentid}')
            vod.etc_abort = 9
            return
        if vod.user_abort and not settings['retry_user_abort']:
            vod.etc_abort = 30
            return
        if vod.user_abort and settings['retry_user_abort']:
            vod.user_abort = False
            vod.etc_abort = 0

        # 제외 에피소드 번호
        if vod.episodenumber:
            for keyword in settings['except_episode_keyword']:
                if keyword in vod.episodenumber:
                    vod.etc_abort = 15
                    return

        # 제외 에피소드 제목
        if vod.episodetitle:
            for keyword in settings['except_episode_episodetitle']:
                if keyword in vod.episodetitle:
                    vod.etc_abort = 16
                    return

        # QVOD (contents_json)
        if vod.content_type == 'onairvod':
            should_download = False
            if settings['qvod_download']:
                should_download = True
            else:
                program_title = vod.programtitle.replace(' ', '')
                for title in settings['download_program_in_qvod']:
                    if title in program_title:
                        should_download = True
                        break
            if not should_download:
                vod.etc_abort = 11
                return
            if not vod.contents_json.get('playtime'):
                P.logger.warning(f'No play time: {vod.contentid} ')
                vod.etc_abort = 33
                return
            match = re.compile(r'Quick\sVOD\s(?P<time>\d{2}\:\d{2})\s').search(vod.episodetitle)
            if match:
                dt_now = datetime.datetime.now()
                dt_tmp = datetime.datetime.strptime(match.group('time'), '%H:%M')
                dt_start = datetime.datetime(dt_now.year, dt_now.month, dt_now.day, dt_tmp.hour, dt_tmp.minute, 0, 0)
                if (dt_now - dt_start).seconds < 0:
                    dt_start = dt_start + datetime.timedelta(days=-1)
                qvod_playtime = vod.contents_json['playtime']
                delta = (dt_now - dt_start).seconds
                if int(qvod_playtime) > delta:
                    vod.etc_abort = 8
                    return
                else:
                    vod.etc_abort = 0
            else:
                vod.etc_abort = 7
                return

        # 다운로드 모드 (contents_json)
        if not vod.programgenre:
            # vod.contents_json.get('genretext')
            P.logger.warning(f'No program genre: {vod.contentid} ')
            vod.etc_abort = 33
            return
        match settings['download_mode']:
            case 'blacklist':
                for channel in settings['except_channel']:
                    if channel in vod.channelname:
                        vod.etc_abort = 12
                        return
                for genre in settings['except_program_genres']:
                    if genre in vod.programgenre:
                        vod.etc_abort = 17
                        return
                program_title = vod.programtitle.replace(' ', '')
                for title in settings['except_program']:
                    if title in program_title:
                        vod.etc_abort = 13
                        return
            case 'whitelist':
                try:
                    episode_num = int(vod.episodenumber)
                except Exception:
                    episode_num = 0
                if settings['whitelist_first_episode_download'] and episode_num == 1:
                    return
                should_download = False
                for genre in settings['whitelist_program_genres']:
                    if genre in vod.programgenre:
                        should_download = True
                        break
                program_title = vod.programtitle.replace(' ', '')
                for title in settings['whitelist_program']:
                    if title in program_title:
                        should_download = True
                        break
                if not should_download:
                    vod.etc_abort = 14
                    return

        # UHD 대기 (streaming_json)
        if not vod.quality:
            # vod.streaming_json.get('quality')
            P.logger.warning(f'No streaming quality: {vod.contentid} ')
            vod.etc_abort = 33
            return
        if vod.quality != settings['quality']:
            if settings['quality'] == '2160p' and vod.quality == '1080p' and settings['uhd_wait']:
                if vod.created_time + datetime.timedelta(minutes=settings['uhd_wait_min']) > datetime.datetime.now():
                    vod.etc_abort = 5
                    return
                else:
                    vod.etc_abort = 0
            else:
                vod.etc_abort = 33
                P.logger.error(f"{vod.quality} of {vod.contentid} is not match with {settings['quality']} of the setting.")
                return
        # finally
        vod.etc_abort = 0

    @property
    def pick_out_settings(self) -> dict:
        return {
            'qvod_download': P.ModelSetting.get_bool(f"{self.name}_qvod_download"),
            'download_program_in_qvod': [programtitle.replace(' ', '') for programtitle in P.ModelSetting.get_list(f"{self.name}_download_program_in_qvod", ',')],
            'download_mode': P.ModelSetting.get(f"{self.name}_download_mode"),
            'except_channel': P.ModelSetting.get_list(f"{self.name}_except_channel", ','),
            'except_program': [program_name.replace(' ', '') for program_name in P.ModelSetting.get_list(f"{self.name}_except_program", ',')],
            'except_program_genres': P.ModelSetting.get_list(f'{self.name}_except_genres', delimeter=','),
            'whitelist_program_genres': P.ModelSetting.get_list(f'{self.name}_whitelist_genres', delimeter=','),
            'whitelist_program': [program_name.replace(' ', '') for program_name in P.ModelSetting.get_list(f"{self.name}_whitelist_program", ',')],
            'whitelist_first_episode_download': P.ModelSetting.get_bool(f"{self.name}_whitelist_first_episode_download"),
            'except_episode_keyword': P.ModelSetting.get_list(f"{self.name}_except_episode_keyword", ','),
            'except_episode_episodetitle': P.ModelSetting.get_list(f"{self.name}_except_episode_episodetitle", ','),
            'quality': P.ModelSetting.get(f"{self.name}_quality"),
            'uhd_wait': P.ModelSetting.get_bool('recent_2160_receive_1080'),
            'uhd_wait_min': P.ModelSetting.get_int('recent_2160_wait_minute'),
            'retry_user_abort': P.ModelSetting.get_bool(f"{self.name}_retry_user_abort"),
        }

    def pick_out_recent_vods(self, vods: Iterable['ModelWavveRecent']) -> None:
        settings = self.pick_out_settings
        for vod in vods:
            try:
                self.pick_out_recent_vod(vod, settings)
            except Exception:
                P.logger.excetion(f"contentid={vod.contentid} vod.title={vod.filename}")
            finally:
                vod.save()

    def retrieve_recent_vod(self, vod: 'ModelWavveRecent', settings: dict) -> None:
        contents_json = SupportWavve.vod_contents_contentid(vod.contentid)
        if not contents_json:
            P.logger.warning(f'Skipped - no content details: {vod.contentid}')
            vod.etc_abort = 33
            vod.retry += 1
            return
        vod.set_contents_json(contents_json)
        action = 'dash' if contents_json.get('drms') else 'hls'
        streaming_data = SupportWavve.streaming(vod.content_type, vod.contentid, settings['quality'], action=action)
        if not streaming_data:
            P.logger.warning(f'Skipped - no streaming data: {vod.contentid}')
            vod.etc_abort = 33
            vod.retry += 1
            return
        vod.set_streaming(streaming_data)
        if 'preview' in streaming_data['playurl']:
            P.logger.debug(f'Skipped - preview content: {vod.contentid}')
            vod.etc_abort = 18
            return

    @property
    def retrieve_settings(self) -> dict:
        return {
            'quality': P.ModelSetting.get(f"{self.name}_quality"),
        }

    def retrieve_recent_vods(self, vods: Iterable['ModelWavveRecent']) -> None:
        settings = self.retrieve_settings
        for vod in vods:
            try:
                P.logger.debug(f'Retrieve vod: {vod.contentid}')
                self.retrieve_recent_vod(vod, settings)
            except Exception:
                P.logger.exception(f"{vod.programtitle} [{vod.episodenumber}] {vod.contentid}")
                vod.retry += 1
            finally:
                vod.save()

    def scheduler_function(self) -> None:
        P.logger.debug(f'Schedule starts...')
        if P.ModelSetting.get_bool(f"{self.name}_auto_db_clear"):
            self.db_delete(P.ModelSetting.get_int(f"{self.name}_auto_db_days"))
        try:
            P.logger.debug(f'Update new vods...')
            self.save_recent_vods(self.get_recent_vods())
        except Exception as e:
            P.logger.exception(str(e))
        # UHD 대기 재시도
        retry_vods = ModelWavveRecent.get_episodes_by_etc_abort(5)
        # QVOD 방송중 재시도
        retry_vods.extend(ModelWavveRecent.get_episodes_by_etc_abort(8))
        # 사용자 중지 재시도
        retry_vods.extend(ModelWavveRecent.get_episodes_by_user_abort(True))
        # 다운로드 도중 실패 재시도
        retry_vods.extend(ModelWavveRecent.get_episodes_by_etc_abort(31))
        P.logger.debug(f'Retry vods...')
        self.pick_out_recent_vods(retry_vods)
        # JSON 데이터 갱신 실패 재시도
        P.logger.debug(f'Retry vods failed while retrieving...')
        for vod in ModelWavveRecent.get_episodes_by_etc_abort(33):
            if vod.retry < P.ModelSetting.get_int(f"{self.name}_max_retry"):
                vod.etc_abort = 0
                vod.save()
            else:
                P.logger.debug(f'Retry limit exceeded: {vod.programtitle} [{vod.episodenumber}] {vod.contentid}')
        # JSON 새로고침
        P.logger.debug(f'Retrieving vods...')
        self.retrieve_recent_vods(ModelWavveRecent.get_episodes_by_etc_abort(0))
        # 최종 점검
        P.logger.debug(f'Pick out vods...')
        self.pick_out_recent_vods(ModelWavveRecent.get_episodes_by_etc_abort(0))
        try:
            if self.schedule_running:
                P.logger.warning(f'Schedule is already running.')
                return
            self.schedule_running = True
            self.schedule_started_at = datetime.datetime.now()
            save_path = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))
            foler_tmp = os.path.join(F.config['path_data'], 'tmp')
            for vod in ModelWavveRecent.get_episodes_by_etc_abort(0):
                try:
                    # 다운로드 준비
                    P.logger.debug(f'Prepare downloading vod: {vod.contentid}')
                    if vod.retry >= P.ModelSetting.get_int(f"{self.name}_max_retry"):
                        P.logger.warning(f'Too many retries: {vod.contentid}')
                        continue

                    if SupportWavve.is_expired(vod.playurl, vod.streaming_json.get('issue')):
                        P.logger.warning(f'The play URL may have expired, retrieve it: {vod.contentid}')
                        self.retrieve_recent_vod(vod, self.retrieve_settings)

                    vod.pf = 0
                    vod.save_path = save_path
                    vod.start_time = datetime.datetime.now()
                    vod.etc_abort = 31
                    # start_time 저장
                    vod.save()
                    callback_id = f'{P.package_name}_{self.name}_{vod.id}'
                    if vod.streaming_json.get('drm'):
                        # dash
                        drm_key_request_properties = vod.streaming_json['play_info'].get('drm_key_request_properties')
                        drm_license_uri = vod.streaming_json['play_info'].get('drm_license_uri')
                        if not (drm_key_request_properties and drm_license_uri):
                            P.logger(f"Could not download this DRM file: {vod.filename}")
                            P.logger.error(vod.streaming_json['play_info'])
                            vod.etc_abort = 0
                            vod.retry += 1
                            vod.save()
                            continue

                        params = {
                            'callback_id': callback_id,
                            'logger' : P.logger,
                            'mpd_url' : vod.playurl,
                            'code' : vod.contentid,
                            'output_filename' : vod.filename,
                            'license_headers' : drm_key_request_properties,
                            'license_url' : drm_license_uri,
                            'mpd_headers': vod.streaming_json['play_info'].get('mpd_headers'),
                            'clean' : True,
                            'folder_tmp': foler_tmp,
                            'folder_output': save_path,
                            'proxies': SupportWavve._SupportWavve__get_proxies(),
                        }
                        downloader_cls = REDownloader if P.ModelSetting.get(f'{self.name}_drm') == 'RE' else WVDownloader
                        downloader = downloader_cls(params, callback_function=self.wvtool_callback_function)
                    else:
                        headers = vod.streaming_json['play_info'].get('headers')
                        match P.ModelSetting.get(f'{self.name}_hls'):
                            case 'RE':
                                downloader = REDownloader({
                                    'callback_id': callback_id,
                                    'logger': P.logger,
                                    'mpd_url': vod.playurl,
                                    'streaming_protocol': 'hls',
                                    'code' : vod.contentid,
                                    'output_filename' : vod.filename,
                                    'license_url': None,
                                    'mpd_headers': headers,
                                    'clean': True,
                                    'folder_tmp': foler_tmp,
                                    'folder_output': save_path,
                                    'proxies': SupportWavve._SupportWavve__get_proxies(),
                                }, self.wvtool_callback_function)
                            case _:
                                downloader = SupportFfmpeg(
                                    SupportWavve.get_prefer_url(vod.playurl, headers),
                                    vod.filename,
                                    save_path=save_path,
                                    headers=headers,
                                    callback_id=callback_id,
                                    callback_function=self.ffmpeg_listener,
                                )
                    # 자막 다운로드
                    download_webvtts(
                        vod.streaming_json.get('subtitles') or [],
                        f"{save_path}/{vod.filename}",
                        P.ModelSetting.get_list(f'{self.name}_subtitle_langs', delimeter=',')
                    )
                    # 다운로드 시작
                    while self.current_download_count > max(P.ModelSetting.get_int(f"{self.name}_ffmpeg_max_count") - 1, 0):
                        P.logger.debug(f'The number of downloading: {self.current_download_count} / {P.ModelSetting.get_int(f"{self.name}_ffmpeg_max_count")}')
                        time.sleep(10)
                        if self.schedule_started_at + datetime.timedelta(hours=1) < datetime.datetime.now():
                            raise Exception(f'다운로드 대기 시간 초과: {vod.contentid}')
                    P.logger.debug(f'Downloading starts: {vod.contentid}')
                    downloader.start()
                    self.current_download_count += 1
                    time.sleep(10)
                except Exception:
                    P.logger.exception(f'Failed while downloading: {vod.contentid}')
                    vod.retry += 1
                    vod.etc_abort = 0
                finally:
                    vod.save()
        except Exception as e:
            P.logger.exception(str(e))
        finally:
            self.schedule_running = False
            P.logger.debug(f'Schedule ends.')

    def migration(self) -> None:
        version = float(P.ModelSetting.get(f'{self.name}_db_version'))
        with F.app.app_context():
            try:
                db_file = F.app.config['SQLALCHEMY_BINDS'][P.package_name].replace('sqlite:///', '').split('?')[0]
                conn = sqlite3.connect(db_file)
                with conn:
                    conn.row_factory = sqlite3.Row
                    cs = conn.cursor()
                    # DB 볼륨 정리
                    cs.execute(f'VACUUM;')
                    if version < 1.1:
                        rows = cs.execute(f'SELECT name FROM pragma_table_info("wavve_recent")').fetchall()
                        cols = [row['name'] for row in rows]
                        if 'keyword' in cols:
                            cs.execute(f'ALTER TABLE "wavve_recent" DROP COLUMN "keyword"')
                        if 'programgenre' not in cols:
                            cs.execute(f'ALTER TABLE "wavve_recent" ADD COLUMN "programgenre" VARCHAR')
                        cs.execute(f'DELETE FROM "wavve_setting" WHERE key = "recent_search_genre"')
                        cs.execute(f'UPDATE "wavve_setting" SET value = "1.1" WHERE key = "recent_db_version"')
            except Exception as e:
                P.logger.exception(str(e))
            finally:
                F.db.session.flush()

    def db_delete(self, day: str | int) -> int:
        return ModelWavveRecent.delete_all(day=day)

    def ffmpeg_listener(self, **arg: dict) -> None:
        #P.logger.debug(f'ffmpeg_listener: {arg}')
        episode = None
        refresh_type = None
        match arg['type']:
            case 'status_change':
                match arg['status']:
                    case SupportFfmpeg.Status.DOWNLOADING:
                        if arg['callback_id'].startswith('wavve_recent'):
                            db_id = arg['callback_id'].split('_')[-1]
                            episode = ModelWavveRecent.get_by_id(db_id)
                        if episode:
                            episode.ffmpeg_status = int(arg['status'])
                            episode.duration = arg['data']['duration']
                            episode.save()
                    case SupportFfmpeg.Status.COMPLETED:
                        pass
                    case SupportFfmpeg.Status.READY:
                        pass
            case 'last':
                if arg['callback_id'].startswith('wavve_recent'):
                    db_id = arg['callback_id'].split('_')[-1]
                    episode = ModelWavveRecent.get_by_id(db_id)
                if episode:
                    episode.ffmpeg_status = int(arg['status'])
                    match arg['status']:
                        case status if status in [
                                    SupportFfmpeg.Status.WRONG_URL,
                                    SupportFfmpeg.Status.WRONG_DIRECTORY,
                                    SupportFfmpeg.Status.ERROR,
                                    SupportFfmpeg.Status.EXCEPTION
                                ]:
                            episode.etc_abort = 1
                        case SupportFfmpeg.Status.USER_STOP:
                            episode.user_abort = True
                            episode.etc_abort = 30
                            P.logger.debug('Status.USER_STOP received..')
                        case SupportFfmpeg.Status.COMPLETED:
                            episode.completed = True
                            episode.end_time = datetime.datetime.now()
                            episode.download_time = (episode.end_time - episode.start_time).seconds
                            episode.filesize = arg['data']['filesize']
                            episode.filesize_str = arg['data']['filesize_str']
                            episode.download_speed = arg['data']['download_speed']
                            episode.etc_abort = 32
                            P.logger.debug('Status.COMPLETED received..')
                        case SupportFfmpeg.Status.TIME_OVER:
                            episode.etc_abort = 2
                        case SupportFfmpeg.Status.PF_STOP:
                            # What is PF?
                            episode.pf = int(arg['data']['current_pf_count'])
                            episode.pf_abort = 1
                        case SupportFfmpeg.Status.FORCE_STOP:
                            episode.etc_abort = 3
                        case SupportFfmpeg.Status.HTTP_FORBIDDEN:
                            episode.etc_abort = 4
                    episode.save()
                    P.logger.debug('LAST commit %s', arg['status'])
                    self.current_download_count -= 1
            case 'log':
                pass
            case 'normal':
                pass

        if refresh_type:
            pass

    def wvtool_callback_function(self, args: dict) -> None:
        #P.logger.debug(f'wvtool_callback_function: {args}')
        db_item = ModelWavveRecent.get_by_id(args['data']['callback_id'].split('_')[-1])

        if not db_item:
            return

        is_last = True
        match args['status']:
            case status if status in ["READY", "SEGMENT_FAIL"]:
                pass
            case 'USER_STOP':
                db_item.user_abort = True
                db_item.etc_abort = 30
                db_item.save()
            case status if status in ["EXIST_OUTPUT_FILEPATH", "COMPLETED"]:
                db_item.completed = True
                db_item.end_time = datetime.datetime.now()
                db_item.download_time = (db_item.end_time - db_item.start_time).seconds
                db_item.etc_abort = 32
                db_item.save()
            case "DOWNLOADING":
                is_last = False
            case "ERROR":
                db_item.completed = False
                db_item.etc_abort = 34
                db_item.save()

        if is_last:
            self.current_download_count -= 1


class ModelWavveRecent(ModelBase):

    P = P
    __tablename__ = f'{P.package_name}_recent'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = F.db.Column(F.db.Integer, primary_key=True)
    created_time = F.db.Column(F.db.DateTime)

    recent_json = F.db.Column(F.db.JSON)
    contents_json = F.db.Column(F.db.JSON)
    streaming_json = F.db.Column(F.db.JSON)

    contentid = F.db.Column(F.db.String)
    content_type = F.db.Column(F.db.String)  # movie, episode
    quality = F.db.Column(F.db.String)
    vod_type = F.db.Column(F.db.String) #general onair
    call = F.db.Column(F.db.String) # normal, recent, program
    drm = F.db.Column(F.db.Boolean)

    channelname = F.db.Column(F.db.String)
    programid = F.db.Column(F.db.String)
    programtitle = F.db.Column(F.db.String)
    releasedate = F.db.Column(F.db.String)
    episodenumber = F.db.Column(F.db.String)
    episodetitle = F.db.Column(F.db.String)
    programgenre = F.db.Column(F.db.String)

    image = F.db.Column(F.db.String)
    playurl = F.db.Column(F.db.String)
    filename = F.db.Column(F.db.String)
    duration = F.db.Column(F.db.Integer)
    start_time = F.db.Column(F.db.DateTime)
    end_time = F.db.Column(F.db.DateTime)
    download_time = F.db.Column(F.db.Integer)
    completed = F.db.Column(F.db.Boolean)
    user_abort = F.db.Column(F.db.Boolean)
    pf_abort = F.db.Column(F.db.Boolean)
    '''
    etc_abort

    00: 처리중
    01: FFMPEG 시작 에러
    02: FFMPEG 시작 타임오버
    03: FFMPEG 강제 중지
    04: FFMPEG HTTP FORBIDDEN
    05: 2160p 대기
    06: 화질 없음
    07: 권한 없음
    08: 퀵VOD 방송중
    09: too many retries (20)
    10:
    11: 패스 - QVOD
    12: 패스 - 제외 채널
    13: 패스 - 제외 프로그램
    14: 화이트리스트 제외
    15: 에피소드 제외 episodenumber
    16: 에피소드 제외 episodetitle
    17: 패스 - 제외 장르
    18: 패스 - 프리뷰
    19:
    20:
    21: many retry
    30: 사용자 중지
    31: 다운로드 중
    32: 다운로드 완료
    33: 데이터 갱신 실패
    34: 다운로드 오류
    '''
    etc_abort = F.db.Column(F.db.Integer) # ffmpeg 원인 1, 채널, 프로그램
    ffmpeg_status = F.db.Column(F.db.Integer)
    temp_path = F.db.Column(F.db.String)
    save_path = F.db.Column(F.db.String)
    pf = F.db.Column(F.db.Integer) # Packet Fail counter
    retry = F.db.Column(F.db.Integer)
    filesize = F.db.Column(F.db.Integer)
    filesize_str = F.db.Column(F.db.String)
    download_speed = F.db.Column(F.db.String)

    def __init__(self, call: str, info: dict, streaming: dict = None, contents: dict = None) -> None:
        self.created_time = datetime.datetime.now()
        self.call = call
        self.completed = False
        self.user_abort = False
        self.pf_abort = False
        self.etc_abort = 0
        self.ffmpeg_status = -1
        self.pf = 0
        self.retry = 0
        self.set_info(info)
        if contents:
            self.set_contents_json(contents)
        if streaming:
            self.set_streaming(streaming)

    def set_contents_json(self, contents_json: dict) -> None:
        self.contents_json = contents_json
        self.programgenre = contents_json.get('genretext') or '일반'
        self.drm = True if self.contents_json['drms'] else False

    def set_info(self, data: dict) -> None:
        self.recent_json = data
        self.channelname = data['channelname']
        self.programid = data['programid']
        self.programtitle = data['programtitle']
        self.contentid = data['contentid']
        self.releasedate = data['releasedate']
        self.episodenumber = data['episodenumber']
        self.episodetitle = data['episodetitle']
        self.image = SiteUtil.normalize_url(data['image'])
        self.vod_type = data['type']
        self.content_type = 'onairvod' if data['type'] == 'onair' else 'vod'

    def set_streaming(self, data: dict) -> None:
        self.streaming_json = data
        self.filename = SupportWavve.get_filename(self.contents_json, data['quality'])
        self.playurl = data['playurl']
        if 'hls' in data['play_info']:
            self.playurl = data['play_info']['hls']
        else:
            self.filename = self.filename.replace('.mp4', '.mkv')
            self.playurl = data['play_info']['uri']
        self.quality = data['quality']

    @classmethod
    def get_episode_by_recent(cls, contentid: str) -> 'ModelWavveRecent':
        with F.app.app_context():
            episode = F.db.session.query(cls) \
                .filter((cls.call == 'recent') | (cls.call == None)) \
                .filter_by(contentid=contentid) \
                .with_for_update().first()
            return episode

    @classmethod
    def get_episodes_by_etc_abort(cls, etc_abort: int) -> list:
        with F.app.app_context():
            return F.db.session.query(cls) \
                .filter((cls.call == 'recent') | (cls.call == None)) \
                .filter_by(etc_abort=etc_abort) \
                .with_for_update().all()

    @classmethod
    def get_episodes_by_user_abort(cls, user_abort: bool) -> list:
        with F.app.app_context():
            return F.db.session.query(cls) \
                .filter((cls.call == 'recent') | (cls.call == None)) \
                .filter_by(user_abort=user_abort) \
                .with_for_update().all()

    # 오버라이딩
    @classmethod
    def make_query(cls, req: flask.Request, order: str = 'desc', search: str = '', option1: str = 'all', option2: str = 'all') -> Query:
        with F.app.app_context():
            query = F.db.session.query(cls)
            if search:
                query = cls.make_query_search(query, search, cls.programtitle)
                #query = query.filter(or_(cls.programtitle.like('%'+search+'%'), cls.channelname.like('%'+search+'%')))

            match option1:
                case 'completed':
                    query = query.filter_by(completed=True)
                case 'uncompleted':
                    query = query.filter_by(etc_abort='31')
                case 'user_abort':
                    query = query.filter_by(user_abort=True)
                case 'pf_abort':
                    query = query.filter_by(pf_abort=True)
                case 'etc_abort_under_10':
                    query = query.filter(cls.etc_abort < 10, cls.etc_abort > 0)
                case 'etc_abort_15':
                    #query = query.filter_by(etc_abort='15')
                    query = query.filter(or_(cls.etc_abort=='15', cls.etc_abort=='16'))
                case _:
                    etc_abort = option1.replace('etc_abort_', '')
                    if etc_abort.isdigit():
                        query = query.filter_by(etc_abort=etc_abort)

            if order == 'desc':
                query = query.order_by(desc(cls.id))
            else:
                query = query.order_by(cls.id)

            return query
