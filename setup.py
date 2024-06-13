from plugin import *

setting = {
    'filepath' : __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '웨이브',
        'list': [
            {
                'uri': 'basic',
                'name': '기본',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'download', 'name': '다운로드'},
                ]
            },
            {
                'uri': 'recent',
                'name': '최근방송 자동',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'list', 'name': '목록'},
                ]
            },
            {
                'uri': 'program',
                'name': '프로그램별 자동',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'select', 'name': '선택'},
                    {'uri': 'queue', 'name': '큐'},
                    {'uri': 'list', 'name': '목록'},
                ]
            },
            {
                'uri': 'log',
                'name': '로그',
            },
        ]
    },
    'default_route': 'normal',
}

P = create_plugin_instance(setting)

from .mod_basic import ModuleBasic
from .mod_program import ModuleProgram
from .mod_recent import ModuleRecent

P.set_module_list([ModuleBasic, ModuleRecent, ModuleProgram])
