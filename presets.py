# -*- coding: utf-8 -*-
"""
站点类型预设

为常见站点程序提供"接口路径 + 成功关键词"的默认值，
用户在添加站点时选择类型即可少填很多内容，实际接口仍以嗅探/cURL为准。
"""

SITE_PRESETS = [
    {
        'key': 'nexusphp',
        'name': 'NexusPHP 系',
        'desc': '国内最常见的PT程序（多数私有站基于此）',
        'contains': '签到成功,已经签到,今日已签,簽到成功,已經簽到',
        'paths': [
            {'label': 'attendance.php（最常见）', 'method': 'GET', 'path': '/attendance.php'},
            {'label': 'attendance.php?action=add', 'method': 'GET', 'path': '/attendance.php?action=add'},
            {'label': 'signin.php', 'method': 'GET', 'path': '/signin.php'},
            {'label': 'addedtime.php（老站）', 'method': 'GET', 'path': '/addedtime.php'},
        ]
    },
    {
        'key': 'unit3d',
        'name': 'Unit3D 系',
        'desc': 'Unit3D / HDInnovations 内核，页面多为 Blade 模板',
        'contains': 'success,bonus,签到',
        'paths': [
            {'label': '/bonus 领取魔力', 'method': 'GET', 'path': '/bonus'},
            {'label': '/bonus/store 商店', 'method': 'GET', 'path': '/bonus/store'},
            {'label': '/api/bonus 接口', 'method': 'POST', 'path': '/api/bonus'},
        ]
    },
    {
        'key': 'gazelle',
        'name': 'Gazelle 系',
        'desc': 'RED/OPS 等站使用的内核，接口多为 AJAX',
        'contains': 'success,ok',
        'paths': [
            {'label': 'ajax 通用接口', 'method': 'GET', 'path': '/ajax.php?action=community_stats'},
            {'label': 'sections 用户信息', 'method': 'GET', 'path': '/sections/user'},
        ]
    },
    {
        'key': 'json_api',
        'name': '通用 JSON 接口',
        'desc': '前后端分离站点，返回 {"success":true} 之类',
        'contains': '',
        'json_path': '$.success',
        'json_equals': 'true',
        'paths': [
            {'label': '/api/sign', 'method': 'POST', 'path': '/api/sign'},
            {'label': '/api/user/checkin', 'method': 'POST', 'path': '/api/user/checkin'},
            {'label': '/api/attendance', 'method': 'GET', 'path': '/api/attendance'},
        ]
    }
]


def get_presets():
    """返回站点类型预设列表"""
    return SITE_PRESETS


def get_preset(key):
    for p in SITE_PRESETS:
        if p['key'] == key:
            return p
    return None
