import os
import threading
import queue
import time
import datetime

import flask
from flask_sqlalchemy.query import Query
from sqlalchemy import desc

from plugin.create_plugin import PluginBase
from plugin.logic_module_base import PluginModuleBase
from plugin.model_base import ModelBase
from plugin.route import default_route_socketio_module
from support.expand.ffmpeg import SupportFfmpeg
from tool import ToolUtil
from support_site import SupportWavve
from wv_tool import WVDownloader

from .setup import F, P
from .downloader import REDownloader, download_webvtts


name = 'program'


class ModuleProgram(PluginModuleBase):

    recent_code = None
    download_queue = None
    download_thread = None
    current_ffmpeg_count = 0

    def __init__(self, P: PluginBase) -> None:
        super(ModuleProgram, self).__init__(P, 'list')
        self.name = name
        self.db_default = {
            f"{P.package_name}_{self.name}_last_list_option": "",
            f"{self.name}_db_version": "1",
            f"{self.name}_recent_code": "",
            f"{self.name}_save_path": "{PATH_DATA}" + os.sep + "download",
            f"{self.name}_make_program_folder": "False",
            f"{self.name}_ffmpeg_max_count": "4",
            f"{self.name}_quality": "1080p",
            f"{self.name}_failed_redownload": "False",
            f"{self.name}_drm": "WV",
            f"{self.name}_subtitle_langs": "all",
            f"{self.name}_hls": "WV",
        }
        self.web_list_model = ModelWavveProgram
        default_route_socketio_module(self, attach='/queue')
        self.previous_analyze = None

    def process_menu(self, page_name: str, req: flask.Request) -> flask.Response:
        arg = P.ModelSetting.to_dict()
        if page_name == 'select':
            arg['code'] = req.args.get('code') or P.ModelSetting.get(f'{self.name}_recent_code')
        return flask.render_template(f'{P.package_name}_{name}_{page_name}.html', arg=arg)

    def process_command(self, command: str, arg1: str, arg2: str, arg3: str, req: flask.Request) -> flask.Response:
        ret = {'ret':'success'}
        match command:
            case 'analyze':
                ret = self.get_module('basic').analyze(arg1)
                P.ModelSetting.set(f"{self.name}_recent_code", arg1)
                self.previous_analyze = ret
            case 'previous_analyze':
                ret['data'] = self.previous_analyze
            case 'get_contents':
                ret = SupportWavve.vod_contents_contentid(arg1)
                ret = SupportWavve.streaming(ret['type'], ret['contentid'], '2160p')
            case 'program_page':
                data = SupportWavve.vod_program_contents_programid(arg1, page=int(arg2))
                ret =  {'url_type': 'program', 'page':arg2, 'code':arg1, 'data' : data}
            case 'download_program':
                _pass = True if arg3.lower() == 'true' else False
                db_item = ModelWavveProgram.get(arg1, arg2)
                if not _pass and db_item:
                    ret['ret'] = 'warning'
                    ret['msg'] = '이미 DB에 있는 항목입니다.'
                elif _pass and db_item and ModelWavveProgram.get_by_id_in_queue(db_item.id):
                    ret['ret'] = 'warning'
                    ret['msg'] = '이미 큐에 있는 항목입니다.'
                else:
                    if not db_item:
                        db_item = ModelWavveProgram(arg1, arg2)
                        db_item.save()
                    db_item.init_for_queue()
                    self.download_queue.put(db_item)
                    ret['msg'] = '다운로드를 추가 하였습니다.'
            case 'download_program_check':
                lists = arg1[:-1].split(',')
                for _ in lists:
                    code, quality = _.split('|')
                    db_item = ModelWavveProgram(code, quality)
                    db_item.save()
                    db_item.init_for_queue()
                    self.download_queue.put(db_item)
                ret['msg'] = f"{len(lists)}개를 추가 하였습니다."
            case 'queue_list':
                ret = [x.as_dict_for_queue() for x in ModelWavveProgram.queue_list]
            case 'program_list_command':
                match arg1:
                    case 'remove_completed':
                        result = ModelWavveProgram.remove_all(True)
                        ret['msg'] = f"{result}개를 삭제하였습니다."
                    case 'remove_incomplete':
                        result = ModelWavveProgram.remove_all(False)
                        ret['msg'] = f"{result}개를 삭제하였습니다."
                    case 'add_incomplete':
                        result = self.retry_download_failed()
                        ret['msg'] = f"{result}개를 추가 하였습니다."
                    case 'remove_one':
                        result = ModelWavveProgram.delete_by_id(arg2)
                        if result:
                            ret['msg'] = '삭제하였습니다.'
                        else:
                            ret['ret'] = 'warning'
                            ret['msg'] = '실패하였습니다.'
            case 'queue_command':
                match arg1:
                    case 'cancel':
                        queue_item = ModelWavveProgram.get_by_id_in_queue(arg2)
                        downloader = WVDownloader if queue_item.is_drm else SupportFfmpeg
                        downloader.stop_by_callback_id(f"wavve_program_{arg2}")
                    case 'reset':
                        if self.download_queue:
                            self.download_queue.queue.clear()
                        for _ in ModelWavveProgram.queue_list:
                            if not _.is_drm and not _.completed and _.contents_json:
                                SupportFfmpeg.stop_by_callback_id(f"wavve_program_{_.id}")
                        ModelWavveProgram.queue_list = []
                    case 'delete_completed':
                        ModelWavveProgram.queue_list = [item for item in ModelWavveProgram.queue_list if not item.completed]
        return flask.jsonify(ret)

    def plugin_load(self) -> None:
        if not self.download_queue:
            self.download_queue = queue.Queue()

        if not self.download_thread:
            self.download_thread = threading.Thread(target=self.download_thread_function, args=())
            self.download_thread.daemon = True
            self.download_thread.start()

        if P.ModelSetting.get_bool(f"{self.name}_failed_redownload"):
            self.retry_download_failed()

    def download_thread_function(self) -> None:
        while True:
            try:
                while True:
                    if not getattr(SupportWavve, "api", None):
                        P.logger.warning(f"Wavve API is not ready...")
                        time.sleep(1)
                        continue
                    if self.current_ffmpeg_count < P.ModelSetting.get_int(f"{self.name}_ffmpeg_max_count"):
                        break
                    time.sleep(5)

                db_item = self.download_queue.get()
                if db_item.cancel:
                    self.download_queue.task_done()
                    continue
                if not db_item:
                    self.download_queue.task_done()
                    continue
                if not db_item.contents_json:
                    contents_json = SupportWavve.vod_contents_contentid(db_item.episode_code)
                    db_item.set_contents_json(contents_json)

                contenttype = 'onairvod' if db_item.contents_json['type'] == 'onair' else 'vod'
                count = 0
                if not db_item.contents_json.get('drms'):
                    action = 'hls'
                    db_item.is_drm = False
                else:
                    action = "dash"
                    db_item.is_drm = True
                while True:
                    count += 1
                    streaming_data = SupportWavve.streaming(contenttype, db_item.episode_code, db_item.quality, action=action)
                    if not streaming_data:
                        time.sleep(20)
                        if count > 3:
                            db_item.ffmpeg_status_kor = 'URL실패'
                            break
                    else:
                        db_item.filename = SupportWavve.get_filename(db_item.contents_json, streaming_data['quality'])
                        break

                if not streaming_data:
                    P.logger.error('No streaming data')
                    db_item.ffmpeg_status = "ERROR"
                    db_item.ffmpeg_status_kor = "스트리밍 정보 없음"
                    db_item.save()
                    self.socketio_callback('status', db_item.as_dict_for_queue())
                    self.download_queue.task_done()
                    continue

                save_path = ToolUtil.make_path(P.ModelSetting.get(f"{self.name}_save_path"))
                folder_tmp = os.path.join(F.config['path_data'], 'tmp')
                callback_id = f"{P.package_name}_{self.name}_{db_item.id}"
                proxies = SupportWavve.api.get_session().proxies
                if streaming_data.get('drm'):
                     # dash
                    drm_key_request_properties = streaming_data['play_info'].get('drm_key_request_properties')
                    drm_license_uri = streaming_data['play_info'].get('drm_license_uri')
                    if not (drm_key_request_properties and drm_license_uri):
                        P.logger.error(f"Could not download this DRM file: {db_item.filename}")
                        P.logger.error(streaming_data['play_info'])
                        db_item.ffmpeg_status = "ERROR"
                        db_item.ffmpeg_status_kor = "DRM 오류"
                        db_item.save()
                        self.socketio_callback('status', db_item.as_dict_for_queue())
                        self.download_queue.task_done()
                        continue

                    params = {
                        'callback_id': callback_id,
                        'logger' : P.logger,
                        'mpd_url' : streaming_data['play_info']['uri'],
                        'code' : db_item.episode_code,
                        'output_filename' : db_item.filename,
                        'license_headers' : drm_key_request_properties,
                        'license_url' : drm_license_uri,
                        'mpd_headers': streaming_data['play_info'].get('mpd_headers'),
                        'clean' : True,
                        'folder_tmp': folder_tmp,
                        'folder_output': save_path,
                        'proxies': proxies,
                    }
                    downloader_cls = REDownloader if P.ModelSetting.get(f'{self.name}_drm') == 'RE' else WVDownloader
                    downloader = downloader_cls(params, callback_function=self.wvtool_callback_function)
                else:
                    uri = streaming_data['play_info'].get('hls') or streaming_data.get('playurl')
                    headers = streaming_data['play_info'].get('headers')
                    match P.ModelSetting.get(f'{self.name}_hls'):
                        case 'RE':
                            downloader = REDownloader({
                                'callback_id': callback_id,
                                'logger': P.logger,
                                'mpd_url':  uri,
                                'streaming_protocol': 'hls',
                                'code' : db_item.episode_code,
                                'output_filename' : db_item.filename,
                                'license_url': None,
                                'mpd_headers': headers,
                                'clean': True,
                                'folder_tmp': folder_tmp,
                                'folder_output': save_path,
                                'proxies': proxies,
                            }, self.wvtool_callback_function)
                        case _:
                            tmp = SupportWavve.get_prefer_url(uri, headers)
                            downloader = SupportFfmpeg(
                                tmp,
                                db_item.filename,
                                save_path=save_path,
                                callback_function=self.ffmpeg_listener,
                                callback_id=callback_id,
                                headers=headers
                            )
                # 자막 다운로드
                download_webvtts(
                    streaming_data.get('subtitles', []),
                    f"{save_path}/{db_item.filename}",
                    P.ModelSetting.get_list(f'{self.name}_subtitle_langs', delimeter=',')
                )
                downloader.start()

                self.current_ffmpeg_count += 1
                self.download_queue.task_done()

            except Exception as e:
                P.logger.exception(str(e))

    def db_delete(self, day: int | str) -> int:
        return ModelWavveProgram.delete_all(day=day)

    def retry_download_failed(self) -> int:
        failed_list = ModelWavveProgram.get_failed()
        for item in failed_list:
            item.init_for_queue()
            self.download_queue.put(item)
        return len(failed_list)

    def ffmpeg_listener(self, **arg) -> None:
        if arg['type'] == 'last':
            self.current_ffmpeg_count += -1

        db_item = ModelWavveProgram.get_by_id_in_queue(arg['callback_id'].split('_')[-1])
        if not db_item:
            return

        db_item.ffmpeg_arg = arg
        db_item.ffmpeg_status = int(arg['status'])
        db_item.ffmpeg_status_kor = str(arg['status'])
        db_item.ffmpeg_percent = arg['data']['percent']
        db_item.is_downloading = True
        ### edit by lapis
        if int(arg['status']) == 7 or \
           arg['data']['percent'] == 100 or \
           str(arg['status']) in ['완료']:
                db_item.completed = True
                db_item.completed_time = datetime.datetime.now()
                db_item.save()
        if arg['type'] == 'last':
            db_item.is_downloading = False

        self.socketio_callback('status', db_item.as_dict_for_queue())

    def wvtool_callback_function(self, args):
        db_item = ModelWavveProgram.get_by_id_in_queue(args['data']['callback_id'].split('_')[-1])

        if not db_item:
            return

        db_item.is_downloading = True
        is_last = True

        match args['status']:
            case status if status in ['READY', 'SEGMENT_FAIL']:
                is_last = False
            case 'EXIST_OUTPUT_FILEPATH':
                db_item.ffmpeg_status_kor = f"{args['data']['output_filename']} 파일이 있습니다."
            case 'USER_STOP':
                db_item.ffmpeg_status_kor = "사용자 중지"
            case 'COMPLETED':
                db_item.ffmpeg_status_kor = f"{args['data']['output_filename']} 다운로드 완료"
            case 'DOWNLOADING':
                is_last = False
                db_item.is_downloading = True
                db_item.ffmpeg_status_kor = "DRM 다운로드중"
            case "ERROR":
                db_item.completed = False
                db_item.etc_abort = 34
                db_item.save()

        if is_last:
            self.current_ffmpeg_count += -1
            db_item.is_downloading = False
            db_item.completed = True
            db_item.completed_time = datetime.datetime.now()
            db_item.save()

        self.socketio_callback('status', db_item.as_dict_for_queue())


class ModelWavveProgram(ModelBase):

    P = P
    __tablename__ = f'{P.package_name}_program'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = F.db.Column(F.db.Integer, primary_key=True)
    created_time = F.db.Column(F.db.DateTime)
    completed_time = F.db.Column(F.db.DateTime)
    contents_json = F.db.Column(F.db.JSON)
    episode_code = F.db.Column(F.db.String)
    program_id = F.db.Column(F.db.String)
    quality = F.db.Column(F.db.String)
    program_title = F.db.Column(F.db.String)
    episode_number = F.db.Column(F.db.String)
    thumbnail = F.db.Column(F.db.String)
    programimage = F.db.Column(F.db.String)
    completed = F.db.Column(F.db.Boolean)

    current_queue_id = 1
    queue_list = []

    def __init__(self, episode_code: str, quality: str) -> None:
        self.episode_code = episode_code
        self.quality = quality
        self.completed = False
        self.created_time = datetime.datetime.now()

    def init_for_queue(self) -> None:
        self.queue_id = self.current_queue_id
        self.current_queue_id += 1
        self.ffmpeg_status = -1
        self.ffmpeg_status_kor = '대기중'
        self.ffmpeg_percent = 0
        self.queue_created_time = datetime.datetime.now().strftime('%m-%d %H:%M:%S')
        self.ffmpeg_data = None
        self.cancel = False
        self.is_drm = False
        self.is_downloading = False
        self.filename = None
        self.queue_list.append(self)

    @classmethod
    def get(cls, episode_code: str, quality: str) -> 'ModelWavveProgram':
        with F.app.app_context():
            return F.db.session.query(ModelWavveProgram).filter_by(
                episode_code=episode_code,
                quality=quality
            ).order_by(desc(cls.id)).first()

    @classmethod
    def is_duplicate(cls, episode_code: str, quality: str) -> bool:
        return bool(cls.get(episode_code, quality))

    def set_contents_json(self, data: dict) -> None:
        self.contents_json = data
        self.program_id = data['programid']
        self.program_title = data['programtitle']
        self.episode_number = data['episodenumber']
        self.thumbnail = data['image']
        self.programimage = data['programimage']
        self.program_id = data['programid']
        self.is_drm = True if data['drms'] else False
        self.save()

    # 오버라이딩
    @classmethod
    def make_query(cls, req: flask.Request, order: str = 'desc', search: str = '', option1: str = 'all', option2: str = 'all') -> Query:
        with F.app.app_context():
            query = F.db.session.query(cls)
            query = cls.make_query_search(query, search, cls.program_title)

            if option1 == 'completed':
                query = query.filter_by(completed=True)
            elif option1 == 'failed':
                query = query.filter_by(completed=False)

            if order == 'desc':
                query = query.order_by(desc(cls.id))
            else:
                query = query.order_by(cls.id)
            return query

    @classmethod
    def remove_all(cls, is_completed: bool = True) -> int: # to remove_all(True/False)
        with F.app.app_context():
            count = F.db.session.query(cls).filter_by(completed=is_completed).delete()
            F.db.session.commit()
            return count

    @classmethod
    def get_failed(cls) -> list:
        with F.app.app_context():
            return F.db.session.query(ModelWavveProgram).filter_by(completed=False).all()

    ### only for queue
    @classmethod
    def get_by_id_in_queue(cls, id) -> 'ModelWavveProgram':
        for _ in cls.queue_list:
            if _.id == int(id):
                return _

    def as_dict_for_queue(self) -> dict:
        ret = super().as_dict()
        ret['queue_id'] = self.queue_id
        ret['ffmpeg_status'] = self.ffmpeg_status
        ret['ffmpeg_status_kor'] = self.ffmpeg_status_kor
        ret['ffmpeg_percent'] = self.ffmpeg_percent
        ret['queue_created_time'] = self.queue_created_time
        ret['contents_json'] = self.contents_json
        ret['ffmpeg_data'] = self.ffmpeg_data
        ret['cancel'] = self.cancel
        ret['is_downloading'] = self.is_downloading
        return ret
